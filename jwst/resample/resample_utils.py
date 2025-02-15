from copy import deepcopy
import logging
import warnings

import numpy as np
from astropy import units as u
import gwcs

from stdatamodels.dqflags import interpret_bit_flags
from stdatamodels.jwst.datamodels.dqflags import pixel

from jwst.assign_wcs.util import wcs_bbox_from_shape
from stcal.alignment import util


log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


__all__ = ['decode_context']


def make_output_wcs(input_models, ref_wcs=None,
                    pscale_ratio=None, pscale=None, rotation=None, shape=None,
                    crpix=None, crval=None):
    """Generate output WCS here based on footprints of all input WCS objects.

    Parameters
    ----------
    input_models : `~jwst.datamodel.ModelLibrary`
        The datamodels to combine into a single output WCS. Each datamodel must
        have a ``meta.wcs.s_region`` attribute.

    ref_wcs : gwcs.WCS, None, optional
        Custom WCS to use as the output WCS. If not provided,
        the reference WCS will be taken as the WCS of the first input model, with
        its bounding box adjusted to encompass all input frames.

    pscale_ratio : float, None, optional
        Ratio of input to output pixel scale. Ignored when ``pscale``
        is provided.

    pscale : float, None, optional
        Absolute pixel scale in degrees. When provided, overrides
        ``pscale_ratio``.

    rotation : float, None, optional
        Position angle of output image Y-axis relative to North.
        A value of 0.0 would orient the final output image to be North up.
        The default of `None` specifies that the images will not be rotated,
        but will instead be resampled in the default orientation for the camera
        with the x and y axes of the resampled image corresponding
        approximately to the detector axes.

    shape : tuple of int, None, optional
        Shape of the image (data array) using ``numpy.ndarray`` convention
        (``ny`` first and ``nx`` second). This value will be assigned to
        ``pixel_shape`` and ``array_shape`` properties of the returned
        WCS object.

    crpix : tuple of float, None, optional
        Position of the reference pixel in the image array. If ``crpix`` is not
        specified, it will be set to the center of the bounding box of the
        returned WCS object.

    crval : tuple of float, None, optional
        Right ascension and declination of the reference pixel. Automatically
        computed if not provided.

    Returns
    -------
    output_wcs : object
        WCS object, with defined domain, covering entire set of input frames
    """
    if ref_wcs is None:
        sregion_list = []
        with input_models:
            for i, model in enumerate(input_models):
                sregion_list.append(model.meta.wcsinfo.s_region)
                if i == 0:
                    example_model = model
                    ref_wcs = example_model.meta.wcs
                    ref_wcsinfo = example_model.meta.wcsinfo.instance
                input_models.shelve(model)
        naxes = ref_wcs.output_frame.naxes

        if naxes != 2:
            msg = ("Output WCS needs 2 spatial axes "
                   f"but the supplied WCS has {naxes} axes.")
            raise RuntimeError(msg)

        output_wcs = util.wcs_from_sregions(
            sregion_list,
            ref_wcs,
            ref_wcsinfo,
            pscale_ratio=pscale_ratio,
            pscale=pscale,
            rotation=rotation,
            shape=shape,
            crpix=crpix,
            crval=crval
        )
        del example_model

    else:
        naxes = ref_wcs.output_frame.naxes
        if naxes != 2:
            msg = ("Output WCS needs 2 spatial axes "
                   f"but the supplied WCS has {naxes} axes.")
            raise RuntimeError(msg)
        output_wcs = deepcopy(ref_wcs)
        if shape is not None:
            output_wcs.array_shape = shape

    # Check that the output data shape has no zero length dimensions
    if not np.prod(output_wcs.array_shape):
        msg = f"Invalid output frame shape: {tuple(output_wcs.array_shape)}"
        raise ValueError(msg)

    return output_wcs


def shape_from_bounding_box(bounding_box):
    """ Return a numpy shape based on the provided bounding_box
    """
    return tuple(int(axs[1] - axs[0] + 0.5) for axs in bounding_box[::-1])


def calc_gwcs_pixmap(in_wcs, out_wcs, shape=None):
    """ Return a pixel grid map from input frame to output frame.
    """
    if shape:
        bb = wcs_bbox_from_shape(shape)
        log.debug("Bounding box from data shape: {}".format(bb))
    else:
        bb = in_wcs.bounding_box
        log.debug("Bounding box from WCS: {}".format(in_wcs.bounding_box))

    grid = gwcs.wcstools.grid_from_bounding_box(bb)
    pixmap = np.dstack(reproject(in_wcs, out_wcs)(grid[0], grid[1]))

    return pixmap


def reproject(wcs1, wcs2):
    """
    Given two WCSs or transforms return a function which takes pixel
    coordinates in the first WCS or transform and computes them in the second
    one. It performs the forward transformation of ``wcs1`` followed by the
    inverse of ``wcs2``.

    Parameters
    ----------
    wcs1, wcs2 : `~astropy.wcs.WCS` or `~gwcs.wcs.WCS`
        WCS objects that have `pixel_to_world_values` and `world_to_pixel_values`
        methods.

    Returns
    -------
    _reproject : func
        Function to compute the transformations.  It takes x, y
        positions in ``wcs1`` and returns x, y positions in ``wcs2``.
    """

    try:
        # Here we want to use the WCS API functions so that a Sliced WCS
        # will work as well. However, the API functions do not accept
        # keyword arguments and `with_bounding_box=False` cannot be passed.
        # We delete the bounding box on a copy of the WCS - yes, inefficient.
        forward_transform = wcs1.pixel_to_world_values
        wcs_no_bbox = deepcopy(wcs2)
        wcs_no_bbox.bounding_box = None
        backward_transform = wcs_no_bbox.world_to_pixel_values
    except AttributeError as err:
        raise TypeError("Input should be a WCS") from err


    def _reproject(x, y):
        sky = forward_transform(x, y)
        flat_sky = []
        for axis in sky:
            flat_sky.append(axis.flatten())
        # Filter out RuntimeWarnings due to computed NaNs in the WCS
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            det = backward_transform(*tuple(flat_sky))
        det_reshaped = []
        for axis in det:
            det_reshaped.append(axis.reshape(x.shape))
        return tuple(det_reshaped)
    return _reproject


def build_driz_weight(model, weight_type=None, good_bits=None):
    """Create a weight map for use by drizzle
    """
    dqmask = build_mask(model.dq, good_bits)

    if weight_type == 'ivm':
        if (model.hasattr("var_rnoise") and model.var_rnoise is not None and
                model.var_rnoise.shape == model.data.shape):
            with np.errstate(divide="ignore", invalid="ignore"):
                inv_variance = model.var_rnoise**-1
            inv_variance[~np.isfinite(inv_variance)] = 1
        else:
            warnings.warn("var_rnoise array not available. Setting drizzle weight map to 1",
                          RuntimeWarning)
            inv_variance = 1.0
        result = inv_variance * dqmask
    elif weight_type == 'exptime':
        if check_for_tmeasure(model):
            exptime = model.meta.exposure.measurement_time
        else:
            exptime = model.meta.exposure.exposure_time
        result = exptime * dqmask
    else:
        result = np.ones(model.data.shape, dtype=model.data.dtype) * dqmask

    return result.astype(np.float32)


def build_mask(dqarr, bitvalue):
    """Build a bit mask from an input DQ array and a bitvalue flag

    In the returned bit mask, 1 is good, 0 is bad
    """
    bitvalue = interpret_bit_flags(bitvalue, mnemonic_map=pixel)

    if bitvalue is None:
        return np.ones(dqarr.shape, dtype=np.uint8)

    bitvalue = np.array(bitvalue).astype(dqarr.dtype)
    return np.logical_not(np.bitwise_and(dqarr, ~bitvalue)).astype(np.uint8)


def is_sky_like(frame):
    # Differentiate between sky-like and cartesian frames
    return u.Unit("deg") in frame.unit or u.Unit("arcsec") in frame.unit


def is_flux_density(bunit):
    """
    Differentiate between surface brightness and flux density data units.

    Parameters
    ----------
    bunit : str or `~astropy.units.Unit`
       Data units, e.g. 'MJy' (is flux density) or 'MJy/sr' (is not).

    Returns
    -------
    bool
        True if the units are equivalent to flux density units.
    """
    try:
        flux_density = u.Unit(bunit).is_equivalent(u.Jy)
    except (ValueError, TypeError):
        flux_density = False
    return flux_density


def decode_context(context, x, y):
    """ Get 0-based indices of input images that contributed to (resampled)
    output pixel with coordinates ``x`` and ``y``.

    Parameters
    ----------
    context: numpy.ndarray
        A 3D `~numpy.ndarray` of integral data type.

    x: int, list of integers, numpy.ndarray of integers
        X-coordinate of pixels to decode (3rd index into the ``context`` array)

    y: int, list of integers, numpy.ndarray of integers
        Y-coordinate of pixels to decode (2nd index into the ``context`` array)

    Returns
    -------

    A list of `numpy.ndarray` objects each containing indices of input images
    that have contributed to an output pixel with coordinates ``x`` and ``y``.
    The length of returned list is equal to the number of input coordinate
    arrays ``x`` and ``y``.

    Examples
    --------

    An example context array for an output image of array shape ``(5, 6)``
    obtained by resampling 80 input images.

    >>> import numpy as np
    >>> from jwst.resample.resample_utils import decode_context
    >>> con = np.array(
    ...     [[[0, 0, 0, 0, 0, 0],
    ...       [0, 0, 0, 36196864, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 537920000, 0, 0, 0]],
    ...      [[0, 0, 0, 0, 0, 0,],
    ...       [0, 0, 0, 67125536, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 163856, 0, 0, 0]],
    ...      [[0, 0, 0, 0, 0, 0],
    ...       [0, 0, 0, 8203, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 0, 0, 0, 0],
    ...       [0, 0, 32865, 0, 0, 0]]],
    ...     dtype=np.int32
    ... )
    >>> decode_context(con, [3, 2], [1, 4])
    [array([ 9, 12, 14, 19, 21, 25, 37, 40, 46, 58, 64, 65, 67, 77]),
     array([ 9, 20, 29, 36, 47, 49, 64, 69, 70, 79])]

    """
    if context.ndim != 3:
        raise ValueError("'context' must be a 3D array.")

    x = np.atleast_1d(x)
    y = np.atleast_1d(y)

    if x.size != y.size:
        raise ValueError("Coordinate arrays must have equal length.")

    if x.ndim != 1:
        raise ValueError("Coordinates must be scalars or 1D arrays.")

    if not (np.issubdtype(x.dtype, np.integer) and
            np.issubdtype(y.dtype, np.integer)):
        raise ValueError('Pixel coordinates must be integer values')

    nbits = 8 * context.dtype.itemsize
    one = np.array(1, context.dtype)
    flags = np.array([one << i for i in range(nbits)])

    idx = []
    for xi, yi in zip(x, y):
        idx.append(
            np.flatnonzero(np.bitwise_and.outer(context[:, yi, xi], flags))
        )

    return idx


def _resample_range(data_shape, bbox=None):
    # Find range of input pixels to resample:
    if bbox is None:
        xmin = ymin = 0
        xmax = data_shape[1] - 1
        ymax = data_shape[0] - 1
    else:
        ((x1, x2), (y1, y2)) = bbox
        xmin = max(0, int(x1 + 0.5))
        ymin = max(0, int(y1 + 0.5))
        xmax = min(data_shape[1] - 1, int(x2 + 0.5))
        ymax = min(data_shape[0] - 1, int(y2 + 0.5))

    return xmin, xmax, ymin, ymax


def check_for_tmeasure(model):
    '''
    Check if the measurement_time keyword is present in the datamodel
    for use in exptime weighting. If not, revert to using exposure_time.
    '''
    try:
        tmeasure = model.meta.exposure.measurement_time
        if tmeasure is not None:
            return 1
        else:
            return 0
    except AttributeError:
        return 0
