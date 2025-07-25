import logging
from collections import OrderedDict

import astropy.units as u
import numpy as np
from astropy.table import QTable
from astropy.time import Time, TimeDelta
from photutils.aperture import ApertureStats, CircularAnnulus, CircularAperture
from stdatamodels.jwst.datamodels import CubeModel

log = logging.getLogger(__name__)

__all__ = ["tso_aperture_photometry"]


def tso_aperture_photometry(
    datamodel, xcenter, ycenter, radius, radius_inner, radius_outer, gain_2d
):
    """
    Create a photometric catalog for TSO imaging observations.

    Parameters
    ----------
    datamodel : `CubeModel`
        The input `CubeModel` of a TSO imaging observation.

    xcenter, ycenter : float
        The ``x`` and ``y`` center of the aperture.

    radius : float
        The radius (in pixels) of the circular aperture.

    radius_inner, radius_outer : float
        The inner and outer radii (in pixels) of the circular-annulus
        aperture, used for local background estimation.

    gain_2d : ndarray
        The gain for all pixels.

    Returns
    -------
    catalog : `~astropy.table.QTable`
        An astropy QTable (Quantity Table) containing the source
        photometry.
    """
    if not isinstance(datamodel, CubeModel):
        raise TypeError("The input data model must be a CubeModel.")

    # For the SUB64P subarray with the WLP8 pupil, the circular aperture
    # extends beyond the image and the circular annulus does not have any
    # overlap with the image.  In that case, we simply sum all values
    # in the array and skip the background subtraction.
    sub64p_wlp8 = False
    if datamodel.meta.instrument.pupil == "WLP8" and datamodel.meta.subarray.name == "SUB64P":
        sub64p_wlp8 = True

    if not sub64p_wlp8:
        phot_aper = CircularAperture((xcenter, ycenter), r=radius)
        bkg_aper = CircularAnnulus((xcenter, ycenter), r_in=radius_inner, r_out=radius_outer)

    if datamodel.meta.bunit_data == "MJy/sr":
        # Convert the input data and errors from MJy/sr to Jy
        factor = 1e6 * datamodel.meta.photometry.pixelarea_steradians
        datamodel.data *= factor
        datamodel.err *= factor
        datamodel.meta.bunit_data = "Jy"
        datamodel.meta.bunit_err = "Jy"
    elif datamodel.meta.bunit_data == "DN/s":
        # Convert the input data and errors from DN/s to electrons
        factor = datamodel.meta.exposure.integration_time * gain_2d
        datamodel.data *= factor
        datamodel.err *= factor
        datamodel.meta.bunit_data = "electron"
        datamodel.meta.bunit_err = "electron"
    else:
        # Unexpected units - leave them as-is
        log.warning(
            f"Unexpected data units: {datamodel.meta.bunit_data}. "
            "Photometry will be produced using the input units."
        )

    aperture_sum = []
    aperture_sum_err = []
    annulus_sum = []
    annulus_sum_err = []

    nimg = datamodel.data.shape[0]

    if sub64p_wlp8:
        info = (
            "Photometry measured as the sum of all values in the "
            "subarray.  No background subtraction was performed."
        )

        for i in np.arange(nimg):
            aperture_sum.append(np.nansum(datamodel.data[i, :, :]))
            aperture_sum_err.append(np.sqrt(np.nansum(datamodel.err[i, :, :] ** 2)))
    else:
        info = (
            f"Photometry measured in a circular aperture of r={radius} "
            "pixels.  Background calculated as the mean in a "
            f"circular annulus with r_inner={radius_inner} pixels and "
            f"r_outer={radius_outer} pixels."
        )
        for i in np.arange(nimg):
            aperstats = ApertureStats(
                datamodel.data[i, :, :], phot_aper, error=datamodel.err[i, :, :]
            )

            annstats = ApertureStats(
                datamodel.data[i, :, :], bkg_aper, error=datamodel.err[i, :, :]
            )

            aperture_sum.append(aperstats.sum)
            aperture_sum_err.append(aperstats.sum_err)
            annulus_sum.append(annstats.sum)
            annulus_sum_err.append(annstats.sum_err)

    aperture_sum = np.array(aperture_sum)
    aperture_sum_err = np.array(aperture_sum_err)
    annulus_sum = np.array(annulus_sum)
    annulus_sum_err = np.array(annulus_sum_err)

    # construct metadata for output table
    meta = OrderedDict()
    meta["instrument"] = datamodel.meta.instrument.name
    meta["detector"] = datamodel.meta.instrument.detector
    meta["channel"] = datamodel.meta.instrument.channel
    meta["subarray"] = datamodel.meta.subarray.name
    meta["filter"] = datamodel.meta.instrument.filter
    meta["pupil"] = datamodel.meta.instrument.pupil
    meta["target_name"] = datamodel.meta.target.catalog_name
    meta["xcenter"] = xcenter
    meta["ycenter"] = ycenter
    ra_icrs, dec_icrs = datamodel.meta.wcs(xcenter, ycenter)
    meta["ra_icrs"] = ra_icrs
    meta["dec_icrs"] = dec_icrs
    meta["apertures"] = info

    # initialize the output table
    tbl = QTable(meta=meta)

    # check for the INT_TIMES table extension
    if hasattr(datamodel, "int_times") and datamodel.int_times is not None:
        nrows = len(datamodel.int_times)
    else:
        nrows = 0
        log.warning("The INT_TIMES table in the input file is missing or empty.")

    # load the INT_TIMES table data
    if nrows > 0:
        shape = datamodel.data.shape
        if len(shape) == 2:
            num_integ = 1
        else:  # len(shape) == 3
            num_integ = shape[0]
        int_start = datamodel.meta.exposure.integration_start
        if int_start is None:
            int_start = 1
            log.warning(f"INTSTART not found; assuming a value of {int_start}")

        # Columns of integration numbers & times of integration from the
        # INT_TIMES table.
        int_num = datamodel.int_times["integration_number"]
        mid_utc = datamodel.int_times["int_mid_MJD_UTC"]
        offset = int_start - int_num[0]  # both are one-indexed
        if offset < 0:
            log.warning(
                "Range of integration numbers in science data extends "
                "outside the range in INT_TIMES table."
            )
            log.warning("Can't use INT_TIMES table.")
            del int_num, mid_utc
            nrows = 0  # flag as bad
        else:
            log.debug("Times are from the INT_TIMES table")
            time_arr = mid_utc[offset : offset + num_integ]
            int_times = Time(time_arr, format="mjd", scale="utc")

    # compute integration time stamps on the fly
    if nrows == 0:
        log.debug("Times computed from EXPSTART and EFFINTTM")
        dt = datamodel.meta.exposure.integration_time
        n_dt = (
            datamodel.meta.exposure.integration_end - datamodel.meta.exposure.integration_start + 1
        )
        dt_arr = np.arange(1, 1 + n_dt) * dt - (dt / 2.0)
        int_dt = TimeDelta(dt_arr, format="sec")
        int_times = Time(datamodel.meta.exposure.start_time, format="mjd") + int_dt

    # populate table columns
    unit = u.Unit(datamodel.meta.bunit_data)
    tbl["MJD"] = int_times.mjd
    tbl["aperture_sum"] = aperture_sum << unit
    tbl["aperture_sum_err"] = aperture_sum_err << unit

    if not sub64p_wlp8:
        tbl["annulus_sum"] = annulus_sum << unit
        tbl["annulus_sum_err"] = annulus_sum_err << unit

        annulus_mean = annulus_sum / bkg_aper.area
        annulus_mean_err = annulus_sum_err / bkg_aper.area
        aperture_bkg = annulus_mean * phot_aper.area
        aperture_bkg_err = annulus_mean_err * phot_aper.area

        tbl["annulus_mean"] = annulus_mean << unit
        tbl["annulus_mean_err"] = annulus_mean_err << unit

        tbl["aperture_bkg"] = aperture_bkg << unit
        tbl["aperture_bkg_err"] = aperture_bkg_err << unit

        net_aperture_sum = aperture_sum - aperture_bkg
        net_aperture_sum_err = np.sqrt(aperture_sum_err**2 + aperture_bkg_err**2)
        tbl["net_aperture_sum"] = net_aperture_sum << unit
        tbl["net_aperture_sum_err"] = net_aperture_sum_err << unit
    else:
        colnames = [
            "annulus_sum",
            "annulus_sum_err",
            "annulus_mean",
            "annulus_mean_err",
            "aperture_bkg",
            "aperture_bkg_err",
        ]
        for col in colnames:
            tbl[col] = np.full(nimg, np.nan)

        tbl["net_aperture_sum"] = aperture_sum << unit
        tbl["net_aperture_sum_err"] = aperture_sum_err << unit

    return tbl
