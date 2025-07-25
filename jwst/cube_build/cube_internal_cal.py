"""Routines for creating IFUCubes with interpolation method = area , coord_system = internal_cal."""

import numpy as np
from stdatamodels.jwst.datamodels import dqflags
from stdatamodels.jwst.transforms.models import _toindex

from jwst.cube_build.cube_match_internal import cube_wrapper_internal  # c extension

__all__ = ["match_det2cube"]


def match_det2cube(
    instrument,
    x,
    y,
    sliceno,
    input_model,
    transform,
    acoord,
    zcoord,
    crval_along,
    crval3,
    cdelt_along,
    cdelt3,
    naxis1,
    naxis2,
):
    """
    Match detector pixels to output plane in local IFU coordinate system.

    This routine assumes a 1-1 mapping in across slice to slice no.
    This routine assumes the output coordinate systems is local IFU plane.
    The user can not change scaling in across slice dimension
    Map the corners of the x, y detector values to a cube defined by local IFU plane.
    In the along slice, lambda plane find the % area of the detector pixel
    which it overlaps with in the cube. For each spaxel record the detector
    pixels that overlap with it - store flux,  % overlap, beta_distance.

    Parameters
    ----------
    x : ndarray
       Detector x pixel values in the slice
    y : ndarray
       Detector y pixel values in the slice
    sliceno : int
       Slice number
    input_model : IFUImage model
       Input calibrated model or file
    transform : transform
       WCS transform to transform x, y to alpha, beta, lambda
    acoord : ndarray
       Array of along slice valuee defining the along slice spatial dimension the  of IFU cube
    zcoord : ndarray
       Array of wavelength values defining the wavelength dimension of the IFU cube
    crval_along : float
       Along slixe reference value in the IFU cube
    crval3 : float
       Wavelength reference value in the IFU cube
    cdelt_along : float
       Along slice reference value in the IFU cube
    cdelt3 : float
       Wavelength sampling in the IFU cube
    naxis1 : int
       Size of axis 1 of the IFU cube
    naxis2 : int
       Size of axis 2 of the IFU cube

    Returns
    -------
    result : tuple
       Results from running c code to determine internal coordinate system IFU Cubes
       Values contained in result:
        instrument_no: integer. NIRSpec = 1, MIRI = 0
        naxis1 & naxis2: output axis of IFU cube
        crval_along : reference value in along slice dimension
        cdelt_along : sampling in along slice dimension
        crval3 : reference value in wavelength dimension
        cdelt3 : wavelength sampling
        a1, a2, a3, a4: Array of corners of pixels holding along slice coordinates
        lam1, lam2, lam3, lam4: Array of corners of pixels holding wavelength coordinates
        acoord: array holding slice coordinates of the IFU internal cube
        zcoord: array holding wavelength coordinates of the IFU internal cube
        ss: array holding the slice number of each pixel
        pixel_flux: array of pixel fluxes
        pixel_err: array of pixel errors
    """
    x = _toindex(x)
    y = _toindex(y)
    pixel_dq = input_model.dq[y, x]

    all_flags = dqflags.pixel["DO_NOT_USE"] + dqflags.pixel["NON_SCIENCE"]
    # find the location of all the values to reject in cube building
    good_data = np.where(np.bitwise_and(pixel_dq, all_flags) == 0)

    # good data holds the location of pixels we want to map to cube
    x = x[good_data]
    y = y[good_data]
    coord1, coord2, lam = transform(x, y)
    valid = ~np.isnan(coord2)
    x = x[valid]
    y = y[valid]

    yy_bot = y
    yy_top = y + 1
    xx_left = x
    xx_right = x + 1
    # along slice dimension is second coordinate returned from transform
    if instrument == "NIRSPEC":
        b1, a1, lam1 = transform(xx_left, yy_bot)
        b2, a2, lam2 = transform(xx_right, yy_bot)
        b3, a3, lam3 = transform(xx_right, yy_top)
        b4, a4, lam4 = transform(xx_left, yy_top)
        # check if units are in microns or meters, if meters convert to microns
        # only need to check one of the wavelengths
        lmax = np.nanmax(lam1.flatten())
        if lmax < 0.0001:
            lam1 = lam1 * 1.0e6
            lam2 = lam2 * 1.0e6
            lam3 = lam3 * 1.0e6
            lam4 = lam4 * 1.0e6

    if instrument == "MIRI":
        a1, b1, lam1 = transform(xx_left, yy_bot)
        a2, b2, lam2 = transform(xx_right, yy_bot)
        a3, b3, lam3 = transform(xx_right, yy_top)
        a4, b4, lam4 = transform(xx_left, yy_top)

    # corners are returned Nanned if outside range of slice
    # fixed the nanned corners when a1,lam1 is valid but
    # adding 1 to x,y pushes data outside BB valid region

    # on the edge out of bounds
    index_good2 = ~np.isnan(a2)
    index_good3 = ~np.isnan(a3)
    index_good4 = ~np.isnan(a4)

    # we need the cases of only all corners valid numbers
    good = np.where(index_good2 & index_good3 & index_good4)
    a1 = a1[good]
    a2 = a2[good]
    a3 = a3[good]
    a4 = a4[good]
    lam1 = lam1[good]
    lam2 = lam2[good]
    lam3 = lam3[good]
    lam4 = lam4[good]
    x = x[good]
    y = y[good]

    # center of first pixel, x,y = 1 for Adrian's equations
    # but we want the pixel corners, x,y values passed into this
    # routine to start at 0
    pixel_flux = input_model.data[y, x]
    pixel_err = input_model.err[y, x]

    # 1-1 mapping in across slice direction (x for NIRSPEC, y for MIRI)
    if instrument == "NIRSPEC":
        ss = sliceno
        instrument_no = 1
    if instrument == "MIRI":
        ss = sliceno
        instrument_no = 0
    result = cube_wrapper_internal(
        instrument_no,
        naxis1,
        naxis2,
        crval_along,
        cdelt_along,
        crval3,
        cdelt3,
        a1,
        a2,
        a3,
        a4,
        lam1,
        lam2,
        lam3,
        lam4,
        acoord,
        zcoord,
        ss,
        pixel_flux,
        pixel_err,
    )

    return result
