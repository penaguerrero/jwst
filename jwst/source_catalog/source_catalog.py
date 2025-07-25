"""Module to calculate the source catalog."""

import logging
import warnings

import astropy.units as u
import numpy as np
from astropy.convolution import Gaussian2DKernel
from astropy.nddata.utils import NoOverlapError, extract_array
from astropy.stats import SigmaClip, gaussian_fwhm_to_sigma
from astropy.table import QTable
from astropy.utils import lazyproperty
from astropy.utils.exceptions import AstropyUserWarning
from photutils.aperture import CircularAnnulus, CircularAperture, aperture_photometry
from scipy import ndimage
from scipy.spatial import KDTree
from stdatamodels.jwst.datamodels import ImageModel

from jwst import __version__ as jwst_version
from jwst.source_catalog._wcs_helpers import pixel_scale_angle_at_skycoord

log = logging.getLogger(__name__)

__all__ = ["JWSTSourceCatalog"]


class JWSTSourceCatalog:
    """
    Class for the JWST source catalog.

    Notes
    -----
    ``model.err`` is assumed to be the total error array corresponding
    to the input science ``model.data`` array. It is assumed to include
    *all* sources of error, including the Poisson error of the sources,
    and have the same shape and units as the science data array.
    """

    def __init__(
        self,
        model,
        catalog,
        kernel_fwhm,
        aperture_params,
        abvega_offset,
        ci_star_thresholds,
    ):
        """
        Initialize the class.

        Parameters
        ----------
        model : `ImageModel`
            The input `ImageModel`. The data is assumed to be background-subtracted.
            Units are expected to be Jy.
        catalog : astropy.table.QTable
            Source catalog table, e.g. as output from tweakreg_catalog.make_tweakreg_catalog.
        kernel_fwhm : float
            The full-width at half-maximum (FWHM) of the 2D Gaussian kernel.
            This is needed to calculate the DAOFind sharpness and roundness
            properties (DAOFind uses a special kernel that sums to zero).
        aperture_params : dict
            A dictionary containing the aperture parameters (radii, aperture
            corrections, and background annulus inner and outer radii).
        abvega_offset : float
            Offset to convert from AB to Vega magnitudes.  The value
            represents m_AB - m_Vega.
        ci_star_thresholds : array-like of 2 floats
            The concentration index thresholds for determining whether
            a source is a star. The first threshold corresponds to the
            concentration index calculated from the smallest and middle
            aperture radii (see ``aperture_params``). The second threshold
            corresponds to the concentration index calculated from the
            middle and largest aperture radii. An object is considered
            extended if both concentration indices are greater than the
            corresponding thresholds, otherwise it is considered a star.
        """
        if not isinstance(model, ImageModel):
            raise TypeError("The input model must be a ImageModel.")
        self.model = model  # background was previously subtracted

        self.segm_cat = catalog
        self.kernel_sigma = kernel_fwhm * gaussian_fwhm_to_sigma
        self.aperture_params = aperture_params
        self.abvega_offset = abvega_offset

        if len(ci_star_thresholds) != 2:
            raise ValueError("ci_star_thresholds must contain only 2 items")
        self.ci_star_thresholds = ci_star_thresholds

        self.n_sources = len(self.segm_cat)
        self.aperture_ee = self.aperture_params["aperture_ee"]
        self.n_aper = len(self.aperture_ee)
        self.wcs = self.model.meta.wcs
        self.column_desc = {}
        self.meta = {}

    @staticmethod
    def convert_mjysr_to_jy(model):
        """Convert data and errors from MJy/sr to Jy, and into `~astropy.unit.Quantity` objects."""
        in_unit = "MJy/sr"
        if model.meta.bunit_data != in_unit or model.meta.bunit_err != in_unit:
            raise ValueError("data and err are expected to be in units of MJy/sr")

        unit = u.Jy
        if model.meta.photometry.pixelarea_steradians is None:
            log.warning("Pixel area is None. Can't convert to Jy.")
        else:
            to_jy = 1.0e6 * model.meta.photometry.pixelarea_steradians
            model.data *= to_jy
            model.err *= to_jy
            model.data <<= unit
            model.err <<= unit
            model.meta.bunit_data = unit.name
            model.meta.bunit_err = unit.name

    @staticmethod
    def convert_jy_to_mjysr(model):
        """Convert data and errors from Jy to MJy/sr, and from `Quantity` to `~np.ndarray`."""
        if model.meta.photometry.pixelarea_steradians is None:
            log.warning("Pixel area is None. Can't convert from Jy.")
        else:
            to_mjy_sr = 1.0e6 * model.meta.photometry.pixelarea_steradians
            model.data /= to_mjy_sr
            model.err /= to_mjy_sr

            model.data = model.data.value  # remove units
            model.err = model.err.value  # remove units

            model.meta.bunit_data = "MJy/sr"
            model.meta.bunit_err = "MJy/sr"

    @staticmethod
    def convert_flux_to_abmag(flux, flux_err):
        """
        Convert flux (and error) to AB magnitude (and error).

        Parameters
        ----------
        flux, flux_err : `~astropy.unit.Quantity` (array-like of floats with units)
            The input flux and error arrays in units of Jy.

        Returns
        -------
        abmag, abmag_err : `~np.ndarray`
            The output AB magnitude and error arrays.
        """
        # ignore RunTimeWarning if flux or flux_err contains NaNs
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            abmag = -2.5 * np.log10(flux.value) + 8.9
        if (flux_err is None) or np.all(np.isnan(flux_err)):
            abmag_err = np.zeros_like(abmag) * np.nan
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                abmag_err = 2.5 * np.log10(1.0 + (flux_err.value / flux.value))

        # handle negative fluxes
        idx = flux.value < 0
        abmag[idx] = np.nan
        abmag_err[idx] = np.nan

        return abmag, abmag_err

    @lazyproperty
    def segment_colnames(self):
        """
        Update the column_desc dictionary to include column names and descriptions.

        Returns
        -------
        colnames : list of str
            A list of the column names that were updated by this function.
        """
        desc = {}
        desc["label"] = "Unique source identification label number"
        desc["xcentroid"] = "X pixel value of the source centroid (0 indexed)"
        desc["ycentroid"] = "Y pixel value of the source centroid (0 indexed)"
        desc["sky_centroid"] = "ICRS Sky coordinate of the source centroid"
        desc["isophotal_flux"] = "Isophotal flux"
        desc["isophotal_flux_err"] = "Isophotal flux error"
        desc["isophotal_abmag"] = "Isophotal AB magnitude"
        desc["isophotal_abmag_err"] = "Isophotal AB magnitude error"
        desc["isophotal_vegamag"] = "Isophotal Vega magnitude"
        desc["isophotal_vegamag_err"] = "Isophotal Vega magnitude error"
        desc["isophotal_area"] = "Isophotal area"
        desc["semimajor_sigma"] = (
            "1-sigma standard deviation along the "
            "semimajor axis of the 2D Gaussian "
            "function that has the same second-order "
            "central moments as the source"
        )
        desc["semiminor_sigma"] = (
            "1-sigma standard deviation along the "
            "semiminor axis of the 2D Gaussian "
            "function that has the same second-order "
            "central moments as the source"
        )
        desc["ellipticity"] = (
            "1 minus the ratio of the 1-sigma lengths of the semimajor and semiminor axes"
        )
        desc["orientation"] = (
            "The angle (degrees) between the positive X "
            "axis and the major axis (increases "
            "counter-clockwise)"
        )
        desc["sky_orientation"] = "The position angle (degrees) from North of the major axis"
        desc["sky_bbox_ll"] = (
            "Sky coordinate of the lower-left vertex of the minimal bounding box of the source"
        )
        desc["sky_bbox_ul"] = (
            "Sky coordinate of the upper-left vertex of the minimal bounding box of the source"
        )
        desc["sky_bbox_lr"] = (
            "Sky coordinate of the lower-right vertex of the minimal bounding box of the source"
        )
        desc["sky_bbox_ur"] = (
            "Sky coordinate of the upper-right vertex of the minimal bounding box of the source"
        )

        self.column_desc.update(desc)

        return list(desc.keys())

    def set_segment_properties(self):
        """
        Calculate the segment-based source photometry and morphologies.

        The values are set as dynamic attributes.
        """
        self.meta.update(self.segm_cat.meta)

        # rename some columns in the output catalog
        prop_names = {}
        prop_names["label"] = "id"
        prop_names["isophotal_flux"] = "flux"
        prop_names["isophotal_flux_err"] = "segment_fluxerr"
        prop_names["isophotal_area"] = "area"

        for column in self.segment_colnames:
            # define the property name
            prop_name = prop_names.get(column, column)
            if prop_name in self.segm_cat.colnames:
                value = self.segm_cat[prop_name]
            else:
                value = getattr(self, prop_name, np.nan)
            setattr(self, column, value)

    @lazyproperty
    def xypos(self):
        """
        Return the (x, y) source centroids defined from the segmentation image.

        Returns
        -------
        xypos : `~numpy.ndarray`
            A 2D array of the (x, y) source positions.
        """
        return np.transpose((self.xcentroid, self.ycentroid))

    @lazyproperty
    def _xypos_finite(self):
        """
        Set non-finite source positions to a large negative value.

        At these unrealistic positions, the aperture will not overlap the data,
        causing the fluxes and errors from the source finder to be NaN.

        Returns
        -------
        xypos : `~numpy.ndarray`
            A 2D array of the (x, y) source positions.
        """
        xypos = self.xypos.copy()
        nanmask = ~np.isfinite(xypos)
        xypos[nanmask] = -1000.0
        return xypos

    @lazyproperty
    def _xypos_nonfinite_mask(self):
        """
        Mask sources with non-finite positions.

        Returns
        -------
        `~numpy.ndarray`
            A 1D boolean mask where `True` values denote sources where
            either the xcentroid or the ycentroid is not finite.
        """
        return ~np.isfinite(self.xypos).all(axis=1)

    @lazyproperty
    def sky_centroid(self):
        """
        Compute the sky coordinate of the source centroid.

        The output coordinate frame is the same as the input ``wcs``.

        Returns
        -------
        `~astropy.coordinates.SkyCoord` or `None`
            The sky coordinate of the source centroid, or `None` if the WCS is not set.
        """
        if self.wcs is None:
            return np.array([None] * self.n_sources)
        return self.wcs.pixel_to_world(self.xcentroid, self.ycentroid)

    @lazyproperty
    def _isophotal_abmag(self):
        """
        Compute the isophotal AB magnitudes and errors if not yet computed.

        Returns
        -------
        (isophotal_abmag, isophotal_abmag_err) : tuple of `np.ndarray`
            The isophotal AB magnitudes and errors.
        """
        return self.convert_flux_to_abmag(self.isophotal_flux, self.isophotal_flux_err)

    @lazyproperty
    def isophotal_abmag(self):
        """
        Return the isophotal AB magnitudes.

        Returns
        -------
        `np.ndarray`
            The isophotal AB magnitudes.
        """
        return self._isophotal_abmag[0]

    @lazyproperty
    def isophotal_abmag_err(self):
        """
        Return the isophotal AB magnitude errors.

        Returns
        -------
        `np.ndarray`
            The isophotal AB magnitudes.
        """
        return self._isophotal_abmag[1]

    @lazyproperty
    def isophotal_vegamag(self):
        """
        Return the isophotal Vega magnitudes.

        Returns
        -------
        `np.ndarray`
            The isophotal Vega magnitudes.
        """
        return self.isophotal_abmag - self.abvega_offset

    @lazyproperty
    def isophotal_vegamag_err(self):
        """
        Return the isophotal Vega magnitude errors.

        Returns
        -------
        `np.ndarray`
            The isophotal Vega magnitude errors.
        """
        return self.isophotal_abmag_err

    @lazyproperty
    def sky_orientation(self):
        """
        Compute the orientation of the source major axis.

        Returns
        -------
        `~astropy.units.Quantity`
            The position angle of the source major axis, measured East of North.
        """
        # NOTE: crpix1 and crpix2 are 1-based values
        skycoord = self.wcs.pixel_to_world(
            self.model.meta.wcsinfo.crpix1 - 1, self.model.meta.wcsinfo.crpix2 - 1
        )
        _, _, angle = pixel_scale_angle_at_skycoord(skycoord, self.wcs)

        return (180.0 * u.deg) - angle + self.orientation

    def _make_aperture_colnames(self, name):
        """
        Make the aperture column names.

        There are separate columns for the flux/magnitude for each of
        the encircled energies and the total flux/magnitude.

        Parameters
        ----------
        name : {'flux', 'abmag', 'vegamag'}
            The name type of the column.

        Returns
        -------
        colnames : list of str
            A list of the output column names.
        """
        colnames = []
        for aper_ee in self.aperture_ee:
            basename = f"aper{aper_ee}_{name}"
            colnames.append(basename)
            colnames.append(f"{basename}_err")
        colnames.extend([f"aper_total_{name}", f"aper_total_{name}_err"])

        return colnames

    def _make_aperture_descriptions(self, name):
        """
        Make aperture column descriptions.

        Parameters
        ----------
        name : {'flux', 'abmag', 'vegamag'}
            The name type of the column.

        Returns
        -------
        descriptions : list of str
            A list of the output column descriptions.
        """
        if name == "flux":
            ftype = "Flux"
            ftype2 = "flux"
        elif name == "abmag":
            ftype = ftype2 = "AB magnitude"
        elif name == "vegamag":
            ftype = ftype2 = "Vega magnitude"

        desc = []
        for aper_ee in self.aperture_ee:
            desc.append(f"{ftype} within the {aper_ee}% encircled energy circular aperture")
            desc.append(f"{ftype} error within the {aper_ee}% encircled energy circular aperture")

        desc.append(
            f"Total aperture-corrected {ftype2} based on the "
            f"{self.aperture_ee[-1]}% encircled energy circular "
            "aperture; should be used only for unresolved sources."
        )
        desc.append(
            f"Total aperture-corrected {ftype2} error based on the "
            f"{self.aperture_ee[-1]}% encircled energy circular "
            "aperture; should be used only for unresolved sources."
        )

        return desc

    @lazyproperty
    def aperture_flux_colnames(self):
        """
        Return the aperture flux column names.

        Returns
        -------
        list of str
            The aperture flux column names.
        """
        return self._make_aperture_colnames("flux")

    @lazyproperty
    def aperture_flux_descriptions(self):
        """
        Return the aperture flux column descriptions.

        Returns
        -------
        list of str
            The aperture flux column descriptions.
        """
        return self._make_aperture_descriptions("flux")

    @lazyproperty
    def aperture_abmag_colnames(self):
        """
        Return the aperture AB magnitude column names.

        Returns
        -------
        list of str
            The aperture AB magnitude column names.
        """
        return self._make_aperture_colnames("abmag")

    @lazyproperty
    def aperture_abmag_descriptions(self):
        """
        Return the aperture AB magnitude column descriptions.

        Returns
        -------
        list of str
            The aperture AB magnitude column descriptions.
        """
        return self._make_aperture_descriptions("abmag")

    @lazyproperty
    def aperture_vegamag_colnames(self):
        """
        Return the aperture Vega magnitude column names.

        Returns
        -------
        list of str
            The aperture Vega magnitude column names.
        """
        return self._make_aperture_colnames("vegamag")

    @lazyproperty
    def aperture_vegamag_descriptions(self):
        """
        Return the aperture Vega magnitude column descriptions.

        Returns
        -------
        list of str
            The aperture Vega magnitude column descriptions.
        """
        return self._make_aperture_descriptions("vegamag")

    @lazyproperty
    def aperture_colnames(self):
        """
        Update the column_desc dictionary to include background flux information.

        Returns
        -------
        colnames : list of str
            A list of the column names that were updated by this function.
        """
        desc = {}
        desc["aper_bkg_flux"] = (
            "The local background value calculated as "
            "the sigma-clipped median value in the "
            "background annulus aperture"
        )
        desc["aper_bkg_flux_err"] = (
            "The standard error of the sigma-clipped median background value"
        )

        for idx, colname in enumerate(self.aperture_flux_colnames):
            desc[colname] = self.aperture_flux_descriptions[idx]
        for idx, colname in enumerate(self.aperture_abmag_colnames):
            desc[colname] = self.aperture_abmag_descriptions[idx]
        for idx, colname in enumerate(self.aperture_vegamag_colnames):
            desc[colname] = self.aperture_vegamag_descriptions[idx]

        self.column_desc.update(desc)

        return list(desc.keys())

    @lazyproperty
    def _aper_local_background(self):
        """
        Estimate the local background and error using a circular annulus aperture.

        The local background is the sigma-clipped median value in the
        annulus.  The background error is the standard error of the
        median, sqrt(pi / 2N) * std.

        Returns
        -------
        bkg_median, bkg_median_err : tuple of `~astropy.unit.Quantity`
            The local background and error.
        """
        bkg_aper = CircularAnnulus(
            self._xypos_finite,
            self.aperture_params["bkg_aperture_inner_radius"],
            self.aperture_params["bkg_aperture_outer_radius"],
        )
        bkg_aper_masks = bkg_aper.to_mask(method="center")
        sigclip = SigmaClip(sigma=3.0)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            warnings.simplefilter("ignore", category=AstropyUserWarning)

            nvalues = []
            bkg_median = []
            bkg_std = []
            for mask in bkg_aper_masks:
                bkg_data = mask.get_values(self.model.data.value)
                values = sigclip(bkg_data, masked=False)
                nvalues.append(values.size)
                bkg_median.append(np.median(values))
                bkg_std.append(np.std(values))

            nvalues = np.array(nvalues)
            bkg_median = np.array(bkg_median)
            # standard error of the median
            bkg_median_err = np.sqrt(np.pi / (2.0 * nvalues)) * np.array(bkg_std)

        bkg_median <<= self.model.data.unit
        bkg_median_err <<= self.model.data.unit

        return bkg_median, bkg_median_err

    @lazyproperty
    def aper_bkg_flux(self):
        """
        Return aperture local background fluxes (per pixel).

        Returns
        -------
        `~astropy.unit.Quantity` (array-like of floats with units)
            The aperture local background fluxes.
        """
        return self._aper_local_background[0]

    @lazyproperty
    def aper_bkg_flux_err(self):
        """
        Return aperture local background flux errors (per pixel).

        Returns
        -------
        `~astropy.unit.Quantity` (array-like of floats with units)
            The aperture local background flux errors.
        """
        return self._aper_local_background[1]

    def set_aperture_properties(self):
        """Calculate the aperture photometry and set the values as dynamic attributes."""
        apertures = [
            CircularAperture(self._xypos_finite, radius)
            for radius in self.aperture_params["aperture_radii"]
        ]
        aper_phot = aperture_photometry(self.model.data, apertures, error=self.model.err)

        for i, aperture in enumerate(apertures):
            flux_col = f"aperture_sum_{i}"
            flux_err_col = f"aperture_sum_err_{i}"

            # subtract the local background measured in the annulus
            aper_phot[flux_col] -= self.aper_bkg_flux * aperture.area

            flux = aper_phot[flux_col]
            flux_err = aper_phot[flux_err_col]
            abmag, abmag_err = self.convert_flux_to_abmag(flux, flux_err)
            vegamag = abmag - self.abvega_offset
            vegamag_err = abmag_err

            idx0 = 2 * i
            idx1 = (2 * i) + 1
            setattr(self, self.aperture_flux_colnames[idx0], flux)
            setattr(self, self.aperture_flux_colnames[idx1], flux_err)
            setattr(self, self.aperture_abmag_colnames[idx0], abmag)
            setattr(self, self.aperture_abmag_colnames[idx1], abmag_err)
            setattr(self, self.aperture_vegamag_colnames[idx0], vegamag)
            setattr(self, self.aperture_vegamag_colnames[idx1], vegamag_err)

    @lazyproperty
    def extras_colnames(self):
        """
        Update the column_desc dictionary to include DAO starfinder computed quantities.

        Returns
        -------
        colnames : list of str
            A list of the column names that were updated by this function.
        """
        desc = {}
        for idx, colname in enumerate(self.ci_colnames):
            desc[colname] = self.ci_colname_descriptions[idx]

        desc["is_extended"] = "Flag indicating whether the source is extended"
        desc["sharpness"] = "The DAOFind source sharpness statistic"
        desc["roundness"] = "The DAOFind source roundness statistic"
        desc["nn_label"] = "The label number of the nearest neighbor"
        desc["nn_dist"] = "The distance in pixels to the nearest neighbor"
        self.column_desc.update(desc)

        return list(desc.keys())

    @lazyproperty
    def _ci_ee_indices(self):
        """
        Return the EE indices for the concentration indices; always in increasing order.

        Returns
        -------
        tuple of tuples
            The EE indices for the concentration indices.
        """
        return (0, 1), (1, 2), (0, 2)

    @lazyproperty
    def ci_colnames(self):
        """
        Return the column names of the three concentration indices.

        Returns
        -------
        list of str
            The concentration indices column names.
        """
        return [f"CI_{self.aperture_ee[j]}_{self.aperture_ee[i]}" for (i, j) in self._ci_ee_indices]

    @lazyproperty
    def ci_colname_descriptions(self):
        """
        Return the concentration indices column descriptions.

        Returns
        -------
        list of str
            The concentration indices column descriptions.
        """
        return [
            "Concentration index calculated as "
            f"({self.aperture_flux_colnames[2 * j]} / "
            f"{self.aperture_flux_colnames[2 * i]})"
            for (i, j) in self._ci_ee_indices
        ]

    @lazyproperty
    def concentration_indices(self):
        """
        Determine the concentration indices.

        These are calculated as the flux ratios of:

            * the middle / smallest aperture radii/EE,
              e.g., CI_50_30 = aper50_flux / aper30_flux
            * the largest / middle aperture radii/EE,
              e.g., CI_70_50 = aper70_flux / aper50_flux
            * the largest / smallest aperture radii/EE,
              e.g., CI_70_30 = aper70_flux / aper30_flux

        Returns
        -------
        list of float
            The concentration indices.
        """
        fluxes = [
            (self.aperture_flux_colnames[2 * j], self.aperture_flux_colnames[2 * i])
            for (i, j) in self._ci_ee_indices
        ]
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", "divide by zero", RuntimeWarning)
            warnings.filterwarnings("ignore", "invalid value", RuntimeWarning)
            concentration = [
                getattr(self, flux1).value / getattr(self, flux2).value for flux1, flux2 in fluxes
            ]
        return concentration

    def set_ci_properties(self):
        """Set the concentration indices as dynamic attributes."""
        for name, value in zip(self.ci_colnames, self.concentration_indices, strict=False):
            setattr(self, name, value)

    @lazyproperty
    def is_extended(self):
        """
        Return a boolean indicating whether the source is extended.

        Returns
        -------
        `~numpy.ndarray`
            A boolean array where `True` values denote extended sources.
        """
        mask1 = self.concentration_indices[0] > self.ci_star_thresholds[0]
        mask2 = self.concentration_indices[1] > self.ci_star_thresholds[1]
        return np.logical_and(mask1, mask2)

    @lazyproperty
    def _kernel_size(self):
        """
        Return the DAOFind kernel size (in both x and y dimensions), always odd.

        Returns
        -------
        int
            The DAOFind kernel size.
        """
        return 2 * int(max(2.0, 1.5 * self.kernel_sigma)) + 1

    @lazyproperty
    def _kernel_center(self):
        """
        Return the DAOFind kernel x/y center.

        Returns
        -------
        int
            The DAOFind kernel center.
        """
        return (self._kernel_size - 1) // 2

    @lazyproperty
    def _kernel_mask(self):
        """
        Make the DAOFind kernel circular mask.

        Returns
        -------
        `~numpy.ndarray`
            A 2D array containing the DAOFind kernel mask, where 1=good pixels and 0=masked pixels.
        """
        yy, xx = np.mgrid[0 : self._kernel_size, 0 : self._kernel_size]
        radius = np.sqrt((xx - self._kernel_center) ** 2 + (yy - self._kernel_center) ** 2)
        return (radius <= max(2.0, 1.5 * self.kernel_sigma)).astype(int)

    @lazyproperty
    def _daofind_kernel(self):
        """
        Compute the DAOFind kernel, a 2D circular Gaussian normalized to have zero sum.

        Returns
        -------
        `~numpy.ndarray`
            The DAOFind kernel.
        """
        size = self._kernel_size
        kernel = Gaussian2DKernel(self.kernel_sigma, x_size=size, y_size=size).array
        kernel /= np.max(kernel)
        kernel *= self._kernel_mask

        # normalize the kernel to zero sum
        npixels = self._kernel_mask.sum()
        denom = np.sum(kernel**2) - (np.sum(kernel) ** 2 / npixels)
        return ((kernel - (kernel.sum() / npixels)) / denom) * self._kernel_mask

    @lazyproperty
    def _daofind_convolved_data(self):
        """
        Convolve the data with the DAOFind kernel.

        Returns
        -------
        `~numpy.ndarray`
            The convolved data.
        """
        return ndimage.convolve(
            self.model.data.value, self._daofind_kernel, mode="constant", cval=0.0
        )

    @lazyproperty
    def _daofind_cutout(self):
        """
        Construct cutouts centered on each source from the input data.

        The cutout size always matches the size of the DAOFind kernel, which has odd dimensions.

        Returns
        -------
        `~numpy.ndarray`
            A 3D array containing 2D cutouts centered on each source from the input data.
        """
        cutout = []
        for xcen, ycen in zip(*np.transpose(self._xypos_finite), strict=False):
            try:
                cutout_ = extract_array(
                    self.model.data, self._daofind_kernel.shape, (ycen, xcen), fill_value=0.0
                )
            except NoOverlapError:
                cutout_ = np.zeros(self._daofind_kernel.shape)
            cutout.append(cutout_)

        return np.array(cutout)  # all cutouts are the same size

    @lazyproperty
    def _daofind_cutout_conv(self):
        """
        Construct cutouts centered on each source from the convolved data.

        The cutout size always matches the size of the DAOFind kernel, which has odd dimensions.

        Returns
        -------
        `~numpy.ndarray`
            A 3D array containing 2D cutouts centered on each source from the convolved data.
        """
        cutout = []
        for xcen, ycen in zip(*np.transpose(self._xypos_finite), strict=False):
            try:
                cutout_ = extract_array(
                    self._daofind_convolved_data,
                    self._daofind_kernel.shape,
                    (ycen, xcen),
                    fill_value=0.0,
                )
            except NoOverlapError:
                cutout_ = np.zeros(self._daofind_kernel.shape)
            cutout.append(cutout_)

        return np.array(cutout)  # all cutouts are the same size

    @lazyproperty
    def sharpness(self):
        """
        Compute the DAOFind source sharpness statistic.

        The sharpness statistic measures the ratio of the difference
        between the height of the central pixel and the mean of the
        surrounding non-bad pixels to the height of the best fitting
        Gaussian function at that point.

        Stars generally have a ``sharpness`` between 0.2 and 1.0.

        Returns
        -------
        `~numpy.ndarray`
            The DAOFind source sharpness statistic.
        """
        npixels = self._kernel_mask.sum() - 1  # exclude the peak pixel
        data_masked = self._daofind_cutout * self._kernel_mask
        data_peak = self._daofind_cutout[:, self._kernel_center, self._kernel_center]
        conv_peak = self._daofind_cutout_conv[:, self._kernel_center, self._kernel_center]

        data_mean = (np.sum(data_masked, axis=(1, 2)) - data_peak) / npixels

        with warnings.catch_warnings():
            # ignore 0 / 0 for non-finite xypos
            warnings.simplefilter("ignore", category=RuntimeWarning)
            return (data_peak - data_mean) / conv_peak

    @lazyproperty
    def roundness(self):
        """
        Compute the DAOFind source roundness statistic based on symmetry.

        The roundness characteristic computes the ratio of a measure of
        the bilateral symmetry of the object to a measure of the
        four-fold symmetry of the object.

        "Round" objects have a ``roundness`` close to 0, generally
        between -1 and 1.

        Returns
        -------
        `~numpy.ndarray`
            The DAOFind source roundness statistic.
        """
        # set the central (peak) pixel to zero
        cutout = self._daofind_cutout_conv.copy()
        cutout[:, self._kernel_center, self._kernel_center] = 0.0

        # calculate the four roundness quadrants
        quad1 = cutout[:, 0 : self._kernel_center + 1, self._kernel_center + 1 :]
        quad2 = cutout[:, 0 : self._kernel_center, 0 : self._kernel_center + 1]
        quad3 = cutout[:, self._kernel_center :, 0 : self._kernel_center]
        quad4 = cutout[:, self._kernel_center + 1 :, self._kernel_center :]

        axis = (1, 2)
        sum2 = (
            -quad1.sum(axis=axis)
            + quad2.sum(axis=axis)
            - quad3.sum(axis=axis)
            + quad4.sum(axis=axis)
        )
        sum2[sum2 == 0] = 0.0

        sum4 = np.abs(cutout).sum(axis=axis)
        sum4[sum4 == 0] = np.nan

        with warnings.catch_warnings():
            # ignore 0 / 0 for non-finite xypos
            warnings.simplefilter("ignore", category=RuntimeWarning)
            return 2.0 * sum2 / sum4

    @lazyproperty
    def _kdtree_query(self):
        """
        Compute the distance in pixels from a source to its nearest neighbor.

        Returns
        -------
        qdist : `~numpy.ndarray`
            The distance in pixels to the nearest neighbor.
        qidx : `~numpy.ndarray`
            The label number of the nearest neighbor.
        """
        if self.n_sources == 1:
            return [np.nan], [np.nan]

        # non-finite xypos causes memory errors on linux, but not MacOS
        tree = KDTree(self._xypos_finite)
        qdist, qidx = tree.query(self._xypos_finite, k=[2])
        return np.transpose(qdist)[0], np.transpose(qidx)[0]

    @lazyproperty
    def nn_label(self):
        """
        Return the label number of the nearest neighbor for each source.

        A label value of -1 is returned if there is only one detected
        source and for sources with a non-finite xcentroid or ycentroid.

        Returns
        -------
        `~numpy.ndarray`
            The label number of the nearest neighbor.
        """
        if self.n_sources == 1:
            return -1

        nn_label = self.label[self._kdtree_query[1]]
        # assign a label of -1 for non-finite xypos
        nn_label[self._xypos_nonfinite_mask] = -1

        return nn_label

    @lazyproperty
    def nn_dist(self):
        """
        Return the distance in pixels to the nearest neighbor for each source.

        Returns NaN if there is only one detected source or for non-finite xypos.

        Returns
        -------
        `~astropy.units.Quantity` (array-like of floats with units)
            The distances in pixels to the nearest neighbors.
        """
        nn_dist = self._kdtree_query[0]
        if self.n_sources == 1:
            return nn_dist * u.pixel

        # assign a distance of np.nan for non-finite xypos
        nn_dist[self._xypos_nonfinite_mask] = np.nan
        return nn_dist * u.pixel

    @lazyproperty
    def aper_total_flux(self):
        """
        Return the aperture-corrected total flux for sources.

        Computed based on the flux in largest aperture.Should be used only for unresolved sources.

        Returns
        -------
        `~astropy.unit.Quantity`
            The aperture-corrected total flux
        """
        idx = self.n_aper - 1  # apcorr for the largest EE (largest radius)
        flux = self.aperture_params["aperture_corrections"][idx] * getattr(
            self, self.aperture_flux_colnames[idx * 2]
        )
        return flux

    @lazyproperty
    def aper_total_flux_err(self):
        """
        Return the aperture-corrected total flux error for sources.

        Computed based on the flux in largest aperture. Should be used only for unresolved sources.

        Returns
        -------
        `~astropy.unit.Quantity`
            The aperture-corrected total flux error
        """
        idx = self.n_aper - 1  # apcorr for the largest EE (largest radius)
        flux_err = self.aperture_params["aperture_corrections"][idx] * getattr(
            self, self.aperture_flux_colnames[idx * 2 + 1]
        )
        return flux_err

    @lazyproperty
    def _abmag_total(self):
        return self.convert_flux_to_abmag(self.aper_total_flux, self.aper_total_flux_err)

    @lazyproperty
    def aper_total_abmag(self):
        """
        Return the aperture-corrected total AB magnitude.

        Should be used only for unresolved sources.

        Returns
        -------
        `~numpy.ndarray`
            The aperture-corrected total AB magnitude.
        """
        return self._abmag_total[0]

    @lazyproperty
    def aper_total_abmag_err(self):
        """
        Return the aperture-corrected total AB magnitude error.

        Should be used only for unresolved sources.

        Returns
        -------
        `~numpy.ndarray`
            The aperture-corrected total AB magnitude error.
        """
        return self._abmag_total[1]

    @lazyproperty
    def aper_total_vegamag(self):
        """
        Return the aperture-corrected total Vega magnitude.

        Should be used only for unresolved sources.

        Returns
        -------
        `~numpy.ndarray`
            The aperture-corrected total Vega magnitude.
        """
        return self.aper_total_abmag - self.abvega_offset

    @lazyproperty
    def aper_total_vegamag_err(self):
        """
        Return the aperture-corrected total Vega magnitude error.

        Should be used only for unresolved sources.

        Returns
        -------
        `~numpy.ndarray`
            The aperture-corrected total Vega magnitude error.
        """
        return self.aper_total_abmag_err

    @lazyproperty
    def colnames(self):
        """
        Return the column names in order for the final source catalog.

        Returns
        -------
        list of str
            The column names.
        """
        colnames = self.segment_colnames[0:4]
        colnames.extend(self.aperture_colnames)
        colnames.extend(self.extras_colnames)
        colnames.extend(self.segment_colnames[4:])
        return colnames

    @staticmethod
    def format_columns(catalog):
        """
        Format the values in the output catalog.

        Parameters
        ----------
        catalog : `~astropy.table.Table`
            The catalog to format.

        Returns
        -------
        result : `~astropy.table.Table`
            The formatted catalog.
        """
        # output formatting requested by the JPWG (2020.02.05)
        for colname in catalog.colnames:
            if colname in ("xcentroid", "ycentroid") or "CI_" in colname:
                catalog[colname].info.format = ".4f"
            if "flux" in colname:
                catalog[colname].info.format = ".6e"
            if (
                "abmag" in colname
                or "vegamag" in colname
                or colname in ("nn_dist", "sharpness", "roundness")
            ):
                catalog[colname].info.format = ".6f"
            if colname in (
                "semimajor_sigma",
                "semiminor_sigma",
                "ellipticity",
                "orientation",
                "sky_orientation",
            ):
                catalog[colname].info.format = ".6f"

        return catalog

    @lazyproperty
    def catalog(self):
        """
        Build the final source catalog.

        Returns
        -------
        catalog : `~astropy.table.Table`
            The final source catalog.
        """
        self.set_segment_properties()
        self.set_aperture_properties()
        self.set_ci_properties()

        catalog = QTable()
        for column in self.colnames:
            catalog[column] = getattr(self, column)
            catalog[column].info.description = self.column_desc[column]

        catalog = self.format_columns(catalog)

        # update metadata
        # photutils 1.11.0 will change the meta dict key name from
        # version to versions; can remove this check when we require
        # photutils >= 1.11.0
        verkeys = ("versions", "version")
        verkey = set(self.meta.keys()).intersection(verkeys).pop()
        verdict = self.meta.get(verkey, None)

        if verdict is not None:
            verdict["jwst"] = jwst_version
            packages = ["Python", "numpy", "scipy", "astropy", "photutils", "gwcs", "jwst"]
            verdict = {key: verdict[key] for key in packages if key in verdict}
            self.meta[verkey] = verdict

        self.meta["aperture_params"] = self.aperture_params
        self.meta["abvega_offset"] = self.abvega_offset
        catalog.meta.update(self.meta)

        # reset units on input model back to MJy/sr
        self.convert_jy_to_mjysr(self.model)

        return catalog
