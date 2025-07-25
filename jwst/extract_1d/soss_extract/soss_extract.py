import logging

import numpy as np
from astropy.nddata.bitmask import bitfield_to_boolean_mask
from scipy.interpolate import CubicSpline, UnivariateSpline
from stdatamodels.jwst import datamodels
from stdatamodels.jwst.datamodels import SossWaveGridModel, dqflags

from jwst.datamodels.utils.tso_multispec import make_tso_specmodel
from jwst.extract_1d.extract import populate_time_keywords
from jwst.extract_1d.soss_extract.atoca import ExtractionEngine, MaskOverlapError
from jwst.extract_1d.soss_extract.atoca_utils import (
    WebbKernel,
    get_wave_p_or_m,
    grid_from_map_with_extrapolation,
    make_combined_adaptive_grid,
    oversample_grid,
    throughput_soss,
)
from jwst.extract_1d.soss_extract.pastasoss import (
    XTRACE_ORD1_LEN,
    XTRACE_ORD2_LEN,
    _get_soss_wavemaps,
)
from jwst.extract_1d.soss_extract.soss_boxextract import (
    box_extract,
    estim_error_nearest_data,
    get_box_weights,
)
from jwst.extract_1d.soss_extract.soss_syscor import make_background_mask, soss_background
from jwst.lib import pipe_utils

log = logging.getLogger(__name__)

ORDER2_SHORT_CUTOFF = 0.58

__all__ = ["get_ref_file_args", "run_extract1d"]


def get_ref_file_args(ref_files):
    """
    Prepare the reference files for the extraction engine.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels, along with values
        for the subarray and pwcpos, i.e. the pupil wheel position.

    Returns
    -------
    tuple
        The reference file args used with the extraction engine:
        (wavemaps, specprofiles, throughputs, kernels)
    """
    pastasoss_ref = ref_files["pastasoss"]
    pad = getattr(pastasoss_ref.traces[0], "padding", 0)
    if pad > 0:
        do_padding = True
    else:
        do_padding = False

    (wavemap_o1, wavemap_o2) = _get_soss_wavemaps(
        pastasoss_ref,
        pwcpos=ref_files["pwcpos"],
        subarray=ref_files["subarray"],
        padding=do_padding,
        padsize=pad,
        spectraces=False,
    )

    # The spectral profiles for order 1 and 2.
    specprofile_ref = ref_files["specprofile"]

    specprofile_o1 = specprofile_ref.profile[0].data
    specprofile_o2 = specprofile_ref.profile[1].data

    prof_shape0, prof_shape1 = specprofile_o1.shape
    wavemap_shape0, wavemap_shape1 = wavemap_o1.shape

    if prof_shape0 != wavemap_shape0:
        pad0 = (prof_shape0 - wavemap_shape0) // 2
        if pad0 > 0:
            specprofile_o1 = specprofile_o1[pad0:-pad0, :]
            specprofile_o2 = specprofile_o2[pad0:-pad0, :]
        elif pad0 < 0:
            wavemap_o1 = wavemap_o1[pad0:-pad0, :]
            wavemap_o2 = wavemap_o2[pad0:-pad0, :]
    if prof_shape1 != wavemap_shape1:
        pad1 = (prof_shape1 - wavemap_shape1) // 2
        if pad1 > 0:
            specprofile_o1 = specprofile_o1[:, pad1:-pad1]
            specprofile_o2 = specprofile_o2[:, pad1:-pad1]
        elif pad1 < 0:
            wavemap_o1 = wavemap_o1[:, pad1:-pad1]
            wavemap_o2 = wavemap_o2[:, pad1:-pad1]

    # The throughput curves for order 1 and 2.
    throughput_index_dict = {}
    for i, throughput in enumerate(pastasoss_ref.throughputs):
        throughput_index_dict[throughput.spectral_order] = i

    throughput_o1 = throughput_soss(
        pastasoss_ref.throughputs[throughput_index_dict[1]].wavelength[:],
        pastasoss_ref.throughputs[throughput_index_dict[1]].throughput[:],
    )
    throughput_o2 = throughput_soss(
        pastasoss_ref.throughputs[throughput_index_dict[2]].wavelength[:],
        pastasoss_ref.throughputs[throughput_index_dict[2]].throughput[:],
    )

    # The spectral kernels.
    speckernel_ref = ref_files["speckernel"]
    n_pix = 2 * speckernel_ref.meta.halfwidth + 1

    # Take the centroid of each trace as a grid to project the WebbKernel
    # WebbKer needs a 2d input, so artificially add axis
    wave_maps = [wavemap_o1, wavemap_o2]
    centroid = {}
    for wv_map, order in zip(wave_maps, [1, 2], strict=True):
        wv_cent = np.zeros(wv_map.shape[1])

        # Get central wavelength as a function of columns
        col, _, wv = _get_trace_1d(ref_files, order)
        wv_cent[col] = wv

        # Set invalid values to zero
        idx_invalid = ~np.isfinite(wv_cent)
        wv_cent[idx_invalid] = 0.0
        centroid[order] = wv_cent

    # Get kernels
    kernels_o1 = WebbKernel(speckernel_ref.wavelengths, speckernel_ref.kernels, centroid[1], n_pix)
    kernels_o2 = WebbKernel(speckernel_ref.wavelengths, speckernel_ref.kernels, centroid[2], n_pix)

    # Make sure that the kernels cover the wavelength maps
    speckernel_wv_range = [np.min(speckernel_ref.wavelengths), np.max(speckernel_ref.wavelengths)]
    valid_wavemap = (speckernel_wv_range[0] <= wavemap_o1) & (wavemap_o1 <= speckernel_wv_range[1])
    wavemap_o1 = np.where(valid_wavemap, wavemap_o1, 0.0)
    valid_wavemap = (speckernel_wv_range[0] <= wavemap_o2) & (wavemap_o2 <= speckernel_wv_range[1])
    wavemap_o2 = np.where(valid_wavemap, wavemap_o2, 0.0)

    return (
        [wavemap_o1, wavemap_o2],
        [specprofile_o1, specprofile_o2],
        [throughput_o1, throughput_o2],
        [kernels_o1, kernels_o2],
    )


def _get_trace_1d(ref_files, order):
    """
    Get the x, y, wavelength of the trace after applying the transform.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels, along with values
        for subarray and pwcpos, i.e. the pupil wheel position.
    order : int
        The spectral order for which to return the trace parameters.

    Returns
    -------
    xtrace, ytrace, wavetrace : array[float]
        The x, y and wavelength of the trace.
    """
    pastasoss_ref = ref_files["pastasoss"]
    pad = getattr(pastasoss_ref.traces[0], "padding", 0)
    if pad > 0:
        do_padding = True
    else:
        do_padding = False

    (_, _), (spectrace_o1, spectrace_o2) = _get_soss_wavemaps(
        pastasoss_ref,
        pwcpos=ref_files["pwcpos"],
        subarray=ref_files["subarray"],
        padding=do_padding,
        padsize=pad,
        spectraces=True,
    )
    if order == 1:
        spectrace = spectrace_o1
        xtrace = np.arange(XTRACE_ORD1_LEN)
    elif order == 2:
        spectrace = spectrace_o2
        xtrace = np.arange(XTRACE_ORD2_LEN)
    else:
        errmsg = f"Order {order} is not covered by Pastasoss reference file!"
        log.error(errmsg)
        raise ValueError(errmsg)

    # CubicSpline requires monotonically increasing x arr
    if spectrace[0][0] - spectrace[0][1] > 0:
        spectrace = np.flip(spectrace, axis=1)

    trace_interp_y = CubicSpline(spectrace[0], spectrace[1])
    trace_interp_wave = CubicSpline(spectrace[0], spectrace[2])
    ytrace = trace_interp_y(xtrace)
    wavetrace = trace_interp_wave(xtrace)
    return xtrace, ytrace, wavetrace


def _estim_flux_first_order(
    scidata_bkg, scierr, scimask, ref_file_args, mask_trace_profile, threshold=1e-4
):
    """
    Roughly estimate the underlying flux of the target spectrum.

    This is done by simply masking out order 2 and retrieving the flux from order 1.

    Parameters
    ----------
    scidata_bkg : array
        A single background subtracted NIRISS SOSS detector image.
    scierr : array
        The uncertainties corresponding to the detector image.
    scimask : array
        Pixel mask to apply to the detector image.
    ref_file_args : tuple
        A tuple of reference file arguments constructed by get_ref_file_args().
    mask_trace_profile : array[bool]
        Mask determining the aperture used for extraction.
        Set to False where the pixel should be extracted.
    threshold : float, optional:
        The pixels with an aperture[order 2] > `threshold` are considered contaminated
        and will be masked. Default is 1e-4.

    Returns
    -------
    func
        A spline estimator that provides the underlying flux as a function of wavelength
    """
    # Unpack ref_file arguments
    wave_maps, spat_pros, thrpts, _ = ref_file_args

    # Define wavelength grid based on order 1 only (so first index)
    wave_grid = grid_from_map_with_extrapolation(wave_maps[0], spat_pros[0], n_os=1)

    # Mask parts contaminated by order 2 based on its spatial profile
    mask = (spat_pros[1] >= threshold) | mask_trace_profile[0]

    # Init extraction without convolution kernel (so extract the spectrum at order 1 resolution)
    ref_file_args = [wave_maps[0]], [spat_pros[0]], [thrpts[0]], [None]
    engine = ExtractionEngine(*ref_file_args, wave_grid, [mask], global_mask=scimask, orders=[1])

    # Extract estimate
    spec_estimate = engine(scidata_bkg, scierr)

    # Interpolate
    idx = np.isfinite(spec_estimate)
    return UnivariateSpline(wave_grid[idx], spec_estimate[idx], k=3, s=0, ext=0)


def _get_native_grid_from_trace(ref_files, spectral_order):
    """
    Make a 1d-grid of the pixels boundary based on the wavelength solution.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels.
    spectral_order : int
        The spectral order for which to return the trace parameters.

    Returns
    -------
    wave : array[float]
        Grid of the pixels boundaries at the native sampling (1d array)
    col : array[int]
        The column number of the pixel
    """
    # From wavelength solution
    col, _, wave = _get_trace_1d(ref_files, spectral_order)

    # Keep only valid solution ...
    idx_valid = np.isfinite(wave)
    # ... and should correspond to subsequent columns
    is_subsequent = np.diff(col[idx_valid]) == 1
    if not is_subsequent.all():
        msg = f"Wavelength solution for order {spectral_order} contains gaps."
        log.warning(msg)
    wave = wave[idx_valid]
    col = col[idx_valid]
    log.debug(f"Wavelength range for order {spectral_order}: ({wave[[0, -1]]})")

    # Sort
    idx_sort = np.argsort(wave)
    wave = wave[idx_sort]
    col = col[idx_sort]

    return wave, col


def _get_grid_from_trace(ref_files, spectral_order, n_os):
    """
    Make a 1d-grid of the pixels boundary based on the wavelength solution.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels.
    spectral_order : int
        The spectral order for which to return the trace parameters.
    n_os : int or array
        The oversampling factor of the wavelength grid used when solving for
        the uncontaminated flux.

    Returns
    -------
    array[float]
        Grid of the pixels boundaries at the native sampling (1d array)
    """
    wave, _ = _get_native_grid_from_trace(ref_files, spectral_order)

    # Use pixel boundaries instead of the center values
    wv_upper_bnd, wv_lower_bnd = get_wave_p_or_m(wave[None, :])
    # `get_wave_p_or_m` returns 2d array, so keep only 1d
    wv_upper_bnd, wv_lower_bnd = wv_upper_bnd[0], wv_lower_bnd[0]
    # Each upper boundary should correspond the the lower boundary
    # of the following pixel, so only need to add the last upper boundary to complete the grid
    wv_upper_bnd, wv_lower_bnd = np.sort(wv_upper_bnd), np.sort(wv_lower_bnd)
    wave_grid = np.append(wv_lower_bnd, wv_upper_bnd[-1])

    # Oversample as needed
    return oversample_grid(wave_grid, n_os=n_os)


def _make_decontamination_grid(ref_files, rtol, max_grid_size, estimate, n_os):
    """
    Create the grid to use for the simultaneous extraction of order 1 and 2.

    The grid is made by:
    1) requiring that it satisfies the oversampling n_os
    2) trying to reach the specified tolerance for the spectral range shared between order 1 and 2
    3) trying to reach the specified tolerance in the rest of spectral range
    The max_grid_size overrules steps 2) and 3), so the precision may not be reached if
    the grid size needed is too large.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels.
    rtol : float
        The relative tolerance needed on a pixel model.
    max_grid_size : int
        Maximum grid size allowed.
    estimate : UnivariateSpline
        Estimate of the target flux as a function of wavelength in microns.
    n_os : int
        The oversampling factor of the wavelength grid used when solving for
        the uncontaminated flux.

    Returns
    -------
    wave_grid : 1d array
        The grid of the pixels boundaries at the native sampling.
    """
    # Build native grid for each  orders.
    spectral_orders = [2, 1]
    grids_ord = {}
    for sp_ord in spectral_orders:
        grids_ord[sp_ord] = _get_grid_from_trace(ref_files, sp_ord, n_os=n_os)

    # Build the list of grids given to make_combined_grid.
    # It must be ordered in increasing priority.
    # 1rst priority: shared wavelengths with order 1 and 2.
    # 2nd priority: remaining red part of order 1
    # 3rd priority: remaining blue part of order 2
    # So, split order 2 in 2 parts, the shared wavelength and the bluemost part
    is_shared = grids_ord[2] >= np.min(grids_ord[1])
    # Make sure order 1 is not more in the blue than order 2
    cond = grids_ord[1] > np.min(grids_ord[2][is_shared])
    grids_ord[1] = grids_ord[1][cond]
    # And make grid list
    all_grids = [grids_ord[2][is_shared], grids_ord[1], grids_ord[2][~is_shared]]

    # Cut order 2 at 0.77 (not smaller than that)
    # because there is no contamination there. Can be extracted afterward.
    # In the red, no cut.
    wv_range = [0.77, np.max(grids_ord[1])]

    # Finally, build the list of corresponding estimates.
    # The estimate for the overlapping part is the order 1 estimate.
    # There is no estimate yet for the blue part of order 2, so give a flat spectrum.
    def flat_fct(wv):
        return np.ones_like(wv)

    all_estimates = [estimate, estimate, flat_fct]

    # Generate the combined grid
    kwargs = {"rtol": rtol, "max_total_size": max_grid_size, "max_iter": 30}
    return make_combined_adaptive_grid(all_grids, all_estimates, wv_range, **kwargs)


def _append_tiktests(test_a, test_b):
    out = {}
    for key in test_a:
        out[key] = np.append(test_a[key], test_b[key], axis=0)

    return out


def _populate_tikho_attr(spec, tiktests, idx, sp_ord):
    spec.spectral_order = sp_ord
    spec.meta.soss_extract1d.type = "TEST"
    spec.meta.soss_extract1d.chi2 = tiktests["chi2"][idx]
    spec.meta.soss_extract1d.chi2_soft_l1 = tiktests["chi2_soft_l1"][idx]
    spec.meta.soss_extract1d.chi2_cauchy = tiktests["chi2_cauchy"][idx]
    spec.meta.soss_extract1d.reg = np.nansum(tiktests["reg"][idx] ** 2)
    spec.meta.soss_extract1d.factor = tiktests["factors"][idx]
    spec.int_num = 0


def _f_to_spec(f_order, grid_order, ref_file_args, pixel_grid, mask, sp_ord):
    """
    Bin the flux to the pixel grid and build a SpecModel.

    Parameters
    ----------
    f_order : np.array
        The solution f_k of the linear system.
    grid_order : np.array
        The wavelength grid of the solution, usually oversampled compared to the pixel grid.
    ref_file_args : list
        The reference file arguments used by the ExtractionEngine.
    pixel_grid : np.array
        The pixel grid to which the flux should be binned.
    mask : np.array
        The mask of the pixels to be extracted.
    sp_ord : int
        The spectral order of the flux.

    Returns
    -------
    spec : SpecModel
        The SpecModel containing the extracted spectrum.
    """
    # Make sure the input is not modified
    ref_file_args = ref_file_args.copy()

    # Build 1d spectrum integrated over pixels
    pixel_grid = pixel_grid[np.newaxis, :]
    ref_file_args[0] = [pixel_grid]  # Wavelength map
    ref_file_args[1] = [np.ones_like(pixel_grid)]  # No spatial profile
    model = ExtractionEngine(
        *ref_file_args,
        wave_grid=grid_order,
        mask_trace_profile=[mask[np.newaxis, :]],
        orders=[sp_ord],
    )
    f_binned = model.rebuild(f_order, fill_value=np.nan)

    pixel_grid = np.squeeze(pixel_grid)
    f_binned = np.squeeze(f_binned)

    # Remove Nans to save space
    is_valid = np.isfinite(f_binned)
    table_size = np.sum(is_valid)
    out_table = np.zeros(table_size, dtype=datamodels.SpecModel().spec_table.dtype)
    out_table["WAVELENGTH"] = pixel_grid[is_valid]
    out_table["FLUX"] = f_binned[is_valid]
    spec = datamodels.SpecModel(spec_table=out_table)
    spec.spectral_order = sp_ord

    return spec


def _build_tracemodel_order(engine, ref_file_args, f_k, i_order, mask, ref_files):
    # Take only the order's specific ref_files
    ref_file_order = [[ref_f[i_order]] for ref_f in ref_file_args]

    # Pre-convolve the extracted flux (f_k) at the order's resolution
    # so that the convolution matrix must not be re-computed.
    flux_order = engine.kernels[i_order].dot(f_k)

    # Then must take the grid after convolution (smaller)
    grid_order = engine.wave_grid_c(i_order)

    # Keep only valid values to make sure there will be no Nans in the order model
    idx_valid = np.isfinite(flux_order)
    grid_order, flux_order = grid_order[idx_valid], flux_order[idx_valid]

    # And give the identity kernel to the Engine (so no convolution)
    ref_file_order[3] = [np.array([1.0])]

    # Spectral order
    sp_ord = i_order + 1

    # Build model of the order
    model = ExtractionEngine(
        *ref_file_order, wave_grid=grid_order, mask_trace_profile=[mask], orders=[sp_ord]
    )

    # Project on detector and save in dictionary
    tracemodel_ord = model.rebuild(flux_order, fill_value=np.nan)

    # Build 1d spectrum integrated over pixels
    pixel_wave_grid, valid_cols = _get_native_grid_from_trace(ref_files, sp_ord)
    spec_ord = _f_to_spec(
        flux_order,
        grid_order,
        ref_file_order,
        pixel_wave_grid,
        np.all(mask, axis=0)[valid_cols],
        sp_ord,
    )

    return tracemodel_ord, spec_ord


def _build_null_spec_table(wave_grid):
    """
    Build a SpecModel of entirely bad values.

    Parameters
    ----------
    wave_grid : np.array
        Input wavelengths

    Returns
    -------
    spec : SpecModel
        Null SpecModel. Flux values are NaN, DQ flags are 1,
        but note that DQ gets overwritten at end of run_extract1d
    """
    wave_grid_cut = wave_grid[wave_grid > ORDER2_SHORT_CUTOFF]
    spec = datamodels.SpecModel()
    spec.spectral_order = 2
    spec.meta.soss_extract1d.type = "OBSERVATION"
    spec.meta.soss_extract1d.factor = np.nan
    spec.spec_table = np.zeros((wave_grid_cut.size,), dtype=datamodels.SpecModel().spec_table.dtype)
    spec.spec_table["WAVELENGTH"] = wave_grid_cut
    spec.spec_table["FLUX"] = np.empty(wave_grid_cut.size) * np.nan
    spec.spec_table["DQ"] = np.ones(wave_grid_cut.size)
    spec.validate()

    return spec


def _model_image(
    scidata_bkg,
    scierr,
    scimask,
    refmask,
    ref_files,
    box_weights,
    tikfac=None,
    threshold=1e-4,
    n_os=2,
    wave_grid=None,
    estimate=None,
    rtol=1e-3,
    max_grid_size=1000000,
):
    """
    Perform the spectral extraction on a single image.

    Parameters
    ----------
    scidata_bkg : array[float]
        A single background subtracted NIRISS SOSS detector image.
    scierr : array[float]
        The uncertainties corresponding to the detector image.
    scimask : array[bool]
        Pixel mask to apply to detector image.
    refmask : array[bool]
        Pixels that should never be reconstructed e.g. the reference pixels.
    ref_files : dict
        A dictionary of the reference file DataModels, along with values for
        subarray and pwcpos, i.e. the pupil wheel position.
    box_weights : dict
        A dictionary of the weights (for each order) used in the box extraction.
        The weights for each order are 2d arrays with the same size as the detector.
    tikfac : float, optional
        The Tikhonov regularization factor used when solving for
        the uncontaminated flux. If not specified, the optimal Tikhonov factor
        is calculated.
    threshold : float
        The threshold value for using pixels based on the spectral profile.
        Default value is 1e-4.
    n_os : int, optional
        The oversampling factor of the wavelength grid used when solving for
        the uncontaminated flux. If not specified, defaults to 2.
    wave_grid : np.ndarray, optional
        Wavelength grid used by ATOCA to model each pixel valid pixel of the detector.
        If not given, the grid is determined based on an estimate of the flux (estimate),
        the relative tolerance (rtol) required on each pixel model and
        the maximum grid size (max_grid_size).
    estimate : UnivariateSpline or None
         Estimate of the target flux as a function of wavelength in microns.
    rtol : float
        The relative tolerance needed on a pixel model. It is used to determine the sampling
        of wave_grid when the input wave_grid is None. Default is 1e-3.
    max_grid_size : int
        Maximum grid size allowed when wave_grid is None.
        Default is 1000000.

    Returns
    -------
    tracemodels : dict
        Dictionary of the modeled detector images for each order.
    tikfac : float
        Optimal Tikhonov factor used in extraction
    logl : float
        Log likelihood value associated with the Tikhonov factor selected.
    wave_grid : 1d array
        The wavelengths at which the spectra were extracted. Same as wave_grid
        if specified as input.
    spec_list : list of SpecModel
        List of the underlying spectra for each integration and order.
        The tikhonov tests are also included.
    """
    # Generate list of orders to simulate from pastasoss trace list
    order_list = []
    for trace in ref_files["pastasoss"].traces:
        order_list.append(f"Order {trace.spectral_order}")

    # Prepare the reference file arguments.
    ref_file_args = get_ref_file_args(ref_files)

    # Some error values are 0, we need to mask those pixels for the extraction engine.
    scimask = scimask | ~(scierr > 0)

    # Define mask based on box aperture
    # (we want to model each contaminated pixels that will be extracted)
    mask_trace_profile = [(~(box_weights[order] > 0)) | (refmask) for order in order_list]

    # Define mask of pixel to model (all pixels inside box aperture)
    global_mask = np.all(mask_trace_profile, axis=0).astype(bool)

    # Rough estimate of the underlying flux
    if (tikfac is None or wave_grid is None) and estimate is None:
        estimate = _estim_flux_first_order(
            scidata_bkg, scierr, scimask, ref_file_args, mask_trace_profile
        )

    # Generate grid based on estimate if not given
    if wave_grid is None:
        log.info(f"wave_grid not given: generating grid based on rtol={rtol}")
        wave_grid = _make_decontamination_grid(ref_files, rtol, max_grid_size, estimate, n_os)
        log.debug(
            f"wave_grid covering from {wave_grid.min()} to {wave_grid.max()}"
            f" with {wave_grid.size} points"
        )
    else:
        log.info("Using previously computed or user specified wavelength grid.")

    # Initialize the Engine.
    engine = ExtractionEngine(
        *ref_file_args,
        wave_grid=wave_grid,
        mask_trace_profile=mask_trace_profile,
        global_mask=scimask,
        threshold=threshold,
    )

    spec_list = []
    if tikfac is None:
        log.info("Solving for the optimal Tikhonov factor.")
        save_tiktests = True

        # Find the tikhonov factor.
        # Initial pass 8 orders of magnitude with 10 grid points.
        guess_factor = engine.estimate_tikho_factors(estimate)
        log_guess = np.log10(guess_factor)
        factors = np.logspace(log_guess - 4, log_guess + 4, 10)
        all_tests = engine.get_tikho_tests(factors, scidata_bkg, scierr)
        tikfac = engine.best_tikho_factor(all_tests, fit_mode="all")

        # Refine across 4 orders of magnitude.
        tikfac = np.log10(tikfac)
        factors = np.logspace(tikfac - 2, tikfac + 2, 20)
        tiktests = engine.get_tikho_tests(factors, scidata_bkg, scierr)
        tikfac = engine.best_tikho_factor(tiktests, fit_mode="d_chi2")
        all_tests = _append_tiktests(all_tests, tiktests)

        # Save spectra in a list of SingleSpecModels for optional output
        for i_order in range(len(order_list)):
            for idx in range(len(all_tests["factors"])):
                f_k = all_tests["solution"][idx, :]
                args = (engine, ref_file_args, f_k, i_order, global_mask, ref_files)
                _, spec_ord = _build_tracemodel_order(*args)
                _populate_tikho_attr(spec_ord, all_tests, idx, i_order + 1)
                spec_ord.meta.soss_extract1d.color_range = "RED"

                # Add the result to spec_list
                spec_list.append(spec_ord)
    else:
        save_tiktests = False

    log.info(f"Using a Tikhonov factor of {tikfac}")

    # Run the extract method of the Engine.
    f_k = engine(scidata_bkg, scierr, tikhonov=True, factor=tikfac)

    # Compute the log-likelihood of the best fit.
    logl = engine.compute_likelihood(f_k, scidata_bkg, scierr)

    log.info(f"Optimal solution has a log-likelihood of {logl}")

    # Create a new instance of the engine for evaluating the trace model.
    # This allows bad pixels and pixels below the threshold to be reconstructed as well.
    # Model the order 1 and order 2 trace separately.
    tracemodels = {}

    for i_order, order in enumerate(order_list):
        log.debug(f"Building the model image of {order}.")

        args = (engine, ref_file_args, f_k, i_order, global_mask, ref_files)
        tracemodel_ord, spec_ord = _build_tracemodel_order(*args)
        spec_ord.meta.soss_extract1d.factor = tikfac
        spec_ord.meta.soss_extract1d.color_range = "RED"
        spec_ord.meta.soss_extract1d.type = "OBSERVATION"

        # Project on detector and save in dictionary
        tracemodels[order] = tracemodel_ord

        # Add the result to spec_list
        spec_list.append(spec_ord)

    # Model the remaining part of order 2
    if ref_files["subarray"] != "SUBSTRIP96":
        idx_order2 = 1
        order = idx_order2 + 1
        order_str = "Order 2"
        log.info("Generate model for blue-most part of order 2")

        # Take only the second order's specific ref_files
        ref_file_order = [[ref_f[idx_order2]] for ref_f in ref_file_args]

        # Mask for the fit. All valid pixels inside box aperture
        mask_fit = mask_trace_profile[idx_order2] | scimask

        # Build 1d spectrum integrated over pixels
        pixel_wave_grid, valid_cols = _get_native_grid_from_trace(ref_files, order)

        # Hardcode wavelength highest boundary as well.
        # Must overlap with lower limit in make_decontamination_grid
        is_in_wv_range = pixel_wave_grid < 0.95
        pixel_wave_grid, valid_cols = pixel_wave_grid[is_in_wv_range], valid_cols[is_in_wv_range]

        # Range of initial tikhonov factors
        tikfac_log_range = np.log10(tikfac) + np.array([-2, 8])

        # Model the remaining part of order 2 with atoca
        try:
            model, spec_ord = _model_single_order(
                scidata_bkg,
                scierr,
                ref_file_order,
                mask_fit,
                global_mask,
                order,
                pixel_wave_grid,
                valid_cols,
                tikfac_log_range,
                save_tiktests=save_tiktests,
            )

        except MaskOverlapError:
            log.error(
                "Not enough unmasked pixels to model the remaining part of order 2."
                " Model and spectrum will be NaN in that spectral region."
            )
            spec_ord = [_build_null_spec_table(pixel_wave_grid)]
            model = np.nan * np.ones_like(scidata_bkg)

        # Keep only pixels from which order 2 contribution
        # is not already modeled.
        already_modeled = np.isfinite(tracemodels[order_str])
        model = np.where(already_modeled, 0.0, model)

        # Add to tracemodels
        both_nan = np.isnan(tracemodels[order_str]) & np.isnan(model)
        tracemodels[order_str] = np.nansum([tracemodels[order_str], model], axis=0)
        tracemodels[order_str][both_nan] = np.nan

        # Add the result to spec_list
        for sp in spec_ord:
            sp.meta.soss_extract1d.color_range = "BLUE"
        spec_list += spec_ord

    return tracemodels, tikfac, logl, wave_grid, spec_list


def _compute_box_weights(ref_files, shape, width):
    """
    Determine the weights for the box extraction.

    Parameters
    ----------
    ref_files : dict
        A dictionary of the reference file DataModels.
    shape : tuple
        The shape of the detector image.
    width : int
        The width of the box aperture.

    Returns
    -------
    box_weights : dict
        A dictionary of the weights for each order.
    wavelengths : dict
        A dictionary of the wavelengths for each order.
    """
    # Generate list of orders from pastasoss trace list
    order_list = []
    for trace in ref_files["pastasoss"].traces:
        order_list.append(trace.spectral_order)

    # Extract each order from order list
    box_weights, wavelengths = {}, {}
    order_str = {order: f"Order {order}" for order in order_list}
    for order_integer in order_list:
        # Order string-name is used more often than integer-name
        order = order_str[order_integer]

        log.debug(f"Compute box weights for {order}.")

        # Define the box aperture
        xtrace, ytrace, wavelengths[order] = _get_trace_1d(ref_files, order_integer)
        box_weights[order] = get_box_weights(ytrace, width, shape, cols=xtrace)

    return box_weights, wavelengths


def _decontaminate_image(scidata_bkg, tracemodels, subarray):
    """
    Perform decontamination of the image based on the trace models.

    Parameters
    ----------
    scidata_bkg : array
        A single background subtracted NIRISS SOSS detector image.
    tracemodels : dict
        Dictionary of the modeled detector images for each order.
    subarray : str
        The subarray used for the observation.

    Returns
    -------
    decontaminated_data : dict
        Dictionary of the decontaminated data for each order.
    """
    # Which orders to extract.
    if subarray == "SUBSTRIP96":
        order_list = [1, 2]
    else:
        order_list = [1, 2, 3]

    order_str = {order: f"Order {order}" for order in order_list}

    # List of modeled orders
    mod_order_list = tracemodels.keys()

    # Create dictionaries for the output images.
    decontaminated_data = {}

    log.debug("Performing the decontamination.")

    # Extract each order from order list
    for order_integer in order_list:
        # Order string-name is used more often than integer-name
        order = order_str[order_integer]

        # Decontaminate using all other modeled orders
        decont = scidata_bkg
        for mod_order in mod_order_list:
            if mod_order != order:
                log.debug(f"Decontaminating {order} from {mod_order} using model.")
                is_valid = np.isfinite(tracemodels[mod_order])
                decont = decont - np.where(is_valid, tracemodels[mod_order], 0.0)

        # Save results
        decontaminated_data[order] = decont

    return decontaminated_data


def _model_single_order(
    data_order,
    err_order,
    ref_file_args,
    mask_fit,
    mask_rebuild,
    order,
    wave_grid,
    valid_cols,
    tikfac_log_range,
    save_tiktests=False,
):
    """
    Extract an output spectrum for a single spectral order using the ATOCA algorithm.

    The Tikhonov factor is derived in two stages: first, ten factors are tested
    spanning tikfac_log_range, and then a further 20 factors are tested across
    2 orders of magnitude in each direction around the best factor from the first stage.
    The best-fitting model and spectrum are reconstructed using the best-fit Tikhonov factor
    and respecting mask_rebuild.

    Parameters
    ----------
    data_order : np.array
        The 2D data array for the spectral order to be extracted.
    err_order : np.array
        The 2D error array for the spectral order to be extracted.
    ref_file_args : list
        The reference file arguments used by the ExtractionEngine.
    mask_fit : np.array
        Mask determining the aperture used for extraction. This typically includes
        detector bad pixels and any pixels that are not part of the trace
    mask_rebuild : np.array
        Mask determining the aperture used for rebuilding the trace. This typically includes
        only pixels that do not belong to either spectral trace, i.e., regions of the detector
        where no real data could exist.
    order : int
        The spectral order to be extracted.
    wave_grid : np.array
        The wavelength grid used to model the data.
    valid_cols : np.array
        The columns of the detector that are valid for extraction.
    tikfac_log_range : list
        The range of Tikhonov factors to test, in log space.
    save_tiktests : bool, optional
        If True, save the intermediate models and spectra for each Tikhonov factor tested.

    Returns
    -------
    model : np.array
        Model derived from the best Tikhonov factor, same shape as data_order.
    spec_list : list of SpecModel
        If save_tiktests is True, returns a list of the model spectra
        for each Tikhonov factor tested,
        with the best-fitting spectrum last in the list.
        If save_tiktests is False, returns a one-element list with the best-fitting spectrum.

    Notes
    -----
    The last spectrum in the list of SpecModels lacks
    the "chi2", "chi2_soft_l1", "chi2_cauchy", and "reg" attributes,
    as these are only calculated for the intermediate models. The last spectrum is not
    necessarily identical to any of the spectra in the list, as it is reconstructed according to
    mask_rebuild instead of fit respecting mask_fit; that is, bad pixels are included.
    """

    # The throughput and kernel is not needed here
    # set them so they have no effect on the extraction.
    def throughput(wavelength):
        return np.ones_like(wavelength)

    kernel = np.array([1.0])
    ref_file_args[2] = [throughput]
    ref_file_args[3] = [kernel]

    # Define wavelength grid with oversampling of 3 (should be enough)
    wave_grid_os = oversample_grid(wave_grid, n_os=3)
    wave_grid_os = wave_grid_os[wave_grid_os > ORDER2_SHORT_CUTOFF]

    # Initialize the Engine.
    engine = ExtractionEngine(
        *ref_file_args,
        wave_grid=wave_grid_os,
        mask_trace_profile=[mask_fit],
        orders=[order],
    )

    # Find the tikhonov factor.
    # Initial pass with tikfac_range.
    factors = np.logspace(tikfac_log_range[0], tikfac_log_range[-1], 10)
    all_tests = engine.get_tikho_tests(factors, data_order, err_order)
    tikfac = engine.best_tikho_factor(tests=all_tests, fit_mode="all")

    # Refine across 4 orders of magnitude.
    tikfac = np.log10(tikfac)
    factors = np.logspace(tikfac - 2, tikfac + 2, 20)
    tiktests = engine.get_tikho_tests(factors, data_order, err_order)
    tikfac = engine.best_tikho_factor(tiktests, fit_mode="d_chi2")
    all_tests = _append_tiktests(all_tests, tiktests)

    # Run the extract method of the Engine.
    f_k_final = engine(data_order, err_order, tikhonov=True, factor=tikfac)

    # Save binned spectra in a list of SingleSpecModels for optional output
    spec_list = []
    if save_tiktests:
        for idx in range(len(all_tests["factors"])):
            f_k = all_tests["solution"][idx, :]

            # Build 1d spectrum integrated over pixels
            spec_ord = _f_to_spec(
                f_k,
                wave_grid_os,
                ref_file_args,
                wave_grid,
                np.all(mask_rebuild, axis=0)[valid_cols],
                order,
            )
            _populate_tikho_attr(spec_ord, all_tests, idx, order)

            # Add the result to spec_list
            spec_list.append(spec_ord)

    # Rebuild trace, including bad pixels
    engine = ExtractionEngine(
        *ref_file_args,
        wave_grid=wave_grid_os,
        mask_trace_profile=[mask_rebuild],
        orders=[order],
    )
    model = engine.rebuild(f_k_final, fill_value=np.nan)

    # Build 1d spectrum integrated over pixels
    spec_ord = _f_to_spec(
        f_k_final,
        wave_grid_os,
        ref_file_args,
        wave_grid,
        np.all(mask_rebuild, axis=0)[valid_cols],
        order,
    )
    spec_ord.meta.soss_extract1d.factor = tikfac
    spec_ord.meta.soss_extract1d.type = "OBSERVATION"

    # Add the result to spec_list
    spec_list.append(spec_ord)
    return model, spec_list


# Remove bad pixels that are not modeled for pixel number
def _extract_image(
    decontaminated_data, scierr, scimask, box_weights, bad_pix="model", tracemodels=None
):
    """
    Perform the box-extraction on the image using the trace model to correct for contamination.

    Parameters
    ----------
    decontaminated_data : array[float]
        A single background subtracted NIRISS SOSS detector image.
    scierr : array[float]
        The uncertainties corresponding to the detector image.
    scimask : array[float]
        Pixel mask to apply to the detector image.
    box_weights : dict
        A dictionary of the weights (for each order) used in the box extraction.
        The weights for each order are 2d arrays with the same size as the detector.
    bad_pix : str
        How to handle the bad pixels. Options are 'masking' and 'model'.
        'masking' will simply mask the bad pixels, such that the number of pixels
        in each column in the box extraction will not be constant, while the
        'model' option uses `tracemodels` to replace the bad pixels.
    tracemodels : dict
        Dictionary of the modeled detector images for each order.

    Returns
    -------
    fluxes, fluxerrs, npixels : dict
        Each output is a dictionary, with each extracted order as a key.
    """
    # Init models with an empty dictionary if not given
    if tracemodels is None:
        tracemodels = {}

    # Which orders to extract (extract the ones with given box aperture).
    order_list = box_weights.keys()

    # Create dictionaries for the output spectra.
    fluxes, fluxerrs, npixels = {}, {}, {}

    log.info("Performing the box extraction.")

    # Extract each order from order list
    for order in order_list:
        log.debug(f"Extracting {order}.")

        # Define the box aperture
        box_w_ord = box_weights[order]

        # Decontaminate using all other modeled orders
        decont = decontaminated_data[order]

        # Deal with bad pixels if required.
        if bad_pix == "model":
            # Model the bad pixels decontaminated image when available
            try:
                # Some pixels might not be modeled by the bad pixel models
                is_modeled = np.isfinite(tracemodels[order])
                # Replace bad pixels
                decont = np.where(scimask & is_modeled, tracemodels[order], decont)

                log.debug(f"Bad pixels in {order} are replaced with trace model.")

                # Replace error estimate of the bad pixels
                # using other valid pixels of similar value.
                # The pixel to be estimated are the masked pixels in the region of extraction
                # with available model.
                extraction_region = box_w_ord > 0
                pix_to_estim = extraction_region & scimask & is_modeled
                # Use only valid pixels (not masked) in the extraction region
                # for the empirical estimation
                valid_pix = extraction_region & ~scimask
                scierr_ord = estim_error_nearest_data(scierr, decont, pix_to_estim, valid_pix)

                # Update the scimask for box extraction:
                # the pixels that are modeled are not masked anymore, so set to False.
                # Note that they have to be in the extraction region
                # to ensure that scierr is also valid
                scimask_ord = np.where(is_modeled, False, scimask)

            except KeyError:
                # Keep same mask and error
                scimask_ord = scimask
                scierr_ord = scierr
                log.warning(
                    f"Bad pixels in {order} will be masked instead of modeled: "
                    "trace model unavailable."
                )
        else:
            scimask_ord = scimask
            scierr_ord = scierr
            log.info(f"Bad pixels in {order} will be masked.")

        # Perform the box extraction and save
        out = box_extract(decont, scierr_ord, scimask_ord, box_w_ord)
        _, fluxes[order], fluxerrs[order], npixels[order] = out

    return fluxes, fluxerrs, npixels


def run_extract1d(
    input_model,
    pastasoss_ref_name,
    specprofile_ref_name,
    speckernel_ref_name,
    subarray,
    soss_filter,
    soss_kwargs,
):
    """
    Run the spectral extraction on NIRISS SOSS data.

    Parameters
    ----------
    input_model : DataModel
        The input DataModel.
    pastasoss_ref_name : str
        Name of the pastasoss reference file.
    specprofile_ref_name : str
        Name of the specprofile reference file.
    speckernel_ref_name : str
        Name of the speckernel reference file.
    subarray : str
        Subarray on which the data were recorded; one of 'SUBSTRIP96',
        'SUBSTRIP256' or 'FULL'.
    soss_filter : str
        Filter in place during observations; one of 'CLEAR' or 'F277W'.
    soss_kwargs : dict
        Dictionary of keyword arguments passed from extract_1d_step.

    Returns
    -------
    output_model : DataModel
        DataModel containing the extracted spectra.
    """
    # Generate the atoca models or not (not necessarily for decontamination)
    generate_model = soss_kwargs["atoca"] or (soss_kwargs["bad_pix"] == "model")

    # Map the order integer names to the string names
    order_str_2_int = {f"Order {order}": order for order in [1, 2, 3]}

    # Read the reference files.
    pastasoss_ref = datamodels.PastasossModel(pastasoss_ref_name)
    specprofile_ref = datamodels.SpecProfileModel(specprofile_ref_name)
    speckernel_ref = datamodels.SpecKernelModel(speckernel_ref_name)

    ref_files = {}
    ref_files["pastasoss"] = pastasoss_ref
    ref_files["specprofile"] = specprofile_ref
    ref_files["speckernel"] = speckernel_ref
    ref_files["subarray"] = subarray
    ref_files["pwcpos"] = input_model.meta.instrument.pupil_position

    # Unpack wave_grid if wave_grid_in was specified.
    wave_grid_in = soss_kwargs["wave_grid_in"]
    if wave_grid_in is not None:
        log.info(f"Loading wavelength grid from {wave_grid_in}.")
        wave_grid = datamodels.SossWaveGridModel(wave_grid_in).wavegrid
        # Make sure it has the correct precision
        wave_grid = wave_grid.astype("float64")
    else:
        # wave_grid will be estimated later in the first call of `_model_image`
        log.info("Wavelength grid was not specified. Setting `wave_grid` to None.")
        wave_grid = None

    # Convert estimate to cubic spline if given.
    # It should be a SpecModel or a file name (string)
    estimate = soss_kwargs.pop("estimate")
    if estimate is not None:
        log.info("Converting the estimate of the flux to spline function.")

        # Convert estimate to cubic spline
        estimate = datamodels.open(estimate)
        wv_estimate = estimate.spec_table["WAVELENGTH"]
        flux_estimate = estimate.spec_table["FLUX"]
        # Keep only finite values
        idx = np.isfinite(flux_estimate)
        estimate = UnivariateSpline(wv_estimate[idx], flux_estimate[idx], k=3, s=0, ext=0)

    # Initialize the output model.
    output_model = datamodels.TSOMultiSpecModel()
    output_model.update(input_model)  # Copy meta data from input to output.

    # Initialize output spectra returned by ATOCA
    # NOTE: these diagnostic spectra are formatted as a simple MultiSpecModel,
    # with integrations in separate spectral extensions.
    output_atoca = datamodels.MultiSpecModel()
    output_atoca.update(input_model)

    # Initialize output references (model of the detector and box aperture weights).
    output_references = datamodels.SossExtractModel()
    output_references.update(input_model)

    all_tracemodels, all_box_weights = {}, {}

    # Convert to Cube if datamodels is an ImageModel
    if isinstance(input_model, datamodels.ImageModel):
        cube_model = datamodels.CubeModel(shape=(1, *input_model.shape))
        cube_model.data = input_model.data[None, :, :]
        cube_model.err = input_model.err[None, :, :]
        cube_model.dq = input_model.dq[None, :, :]
        nimages = 1
        log.info("Input is an ImageModel, processing a single integration.")

    elif isinstance(input_model, datamodels.CubeModel):
        cube_model = input_model
        nimages = len(cube_model.data)
        log.info(f"Input is a CubeModel containing {nimages} integrations.")

    else:
        msg = "Only ImageModel and CubeModel are implemented for the NIRISS SOSS extraction."
        log.critical(msg)
        raise TypeError(msg)

    # Loop over images.
    output_spec_list = {}
    for i in range(nimages):
        log.info(f"Processing integration {i + 1} of {nimages}.")

        # Unpack the i-th image, set dtype to float64 and convert DQ to boolean mask.
        scidata = cube_model.data[i].astype("float64")
        scierr = cube_model.err[i].astype("float64")
        scimask = np.bitwise_and(cube_model.dq[i], dqflags.pixel["DO_NOT_USE"]).astype(bool)
        refmask = bitfield_to_boolean_mask(
            cube_model.dq[i], ignore_flags=dqflags.pixel["REFERENCE_PIXEL"], flip_bits=True
        )

        # Make sure there aren't any nans not flagged in scimask
        not_finite = ~(np.isfinite(scidata) & np.isfinite(scierr))
        if (not_finite & ~scimask).any():
            log.warning(
                "Input contains invalid values that "
                "are not flagged correctly in the dq map. "
                "They will be masked for the following procedure."
            )
            scimask |= not_finite
            refmask &= ~not_finite

        # Perform background correction.
        if soss_kwargs["subtract_background"]:
            log.info("Applying background subtraction.")
            bkg_mask = make_background_mask(scidata, width=40)
            scidata_bkg, col_bkg = soss_background(scidata, scimask, bkg_mask)
        else:
            log.info("Skip background subtraction.")
            scidata_bkg = scidata
            col_bkg = np.zeros(scidata.shape[1])

        # Pre-compute the weights for box extraction (used in modeling and extraction)
        args = (ref_files, scidata_bkg.shape)
        box_weights, wavelengths = _compute_box_weights(*args, width=soss_kwargs["width"])

        # FIXME: hardcoding the substrip96 weights to unity is a band-aid solution
        if subarray == "SUBSTRIP96":
            box_weights["Order 2"] = np.ones((96, 2048))

        # Model the traces based on optics filter configuration (CLEAR or F277W)
        if soss_filter == "CLEAR" and generate_model:
            # Model the image.
            kwargs = {}
            kwargs["estimate"] = estimate
            kwargs["tikfac"] = soss_kwargs["tikfac"]
            kwargs["max_grid_size"] = soss_kwargs["max_grid_size"]
            kwargs["rtol"] = soss_kwargs["rtol"]
            kwargs["n_os"] = soss_kwargs["n_os"]
            kwargs["wave_grid"] = wave_grid
            kwargs["threshold"] = soss_kwargs["threshold"]

            result = _model_image(
                scidata_bkg, scierr, scimask, refmask, ref_files, box_weights, **kwargs
            )
            tracemodels, soss_kwargs["tikfac"], _, wave_grid, spec_list = result

            # Add atoca spectra to multispec for output
            for spec in spec_list:
                # If it was a test, not the best spectrum,
                # int_num is already set to 0.
                if not hasattr(spec, "int_num"):
                    spec.int_num = i + 1
                output_atoca.spec.append(spec)

        elif soss_filter != "CLEAR" and generate_model:
            # No model can be fit for F277W yet, missing throughput reference files.
            msg = f"No extraction possible for filter {soss_filter}."
            log.critical(msg)
            raise ValueError(msg)
        else:
            # Return empty tracemodels
            tracemodels = {}

        # Decontaminate the data using trace models (if tracemodels not empty)
        data_to_extract = _decontaminate_image(scidata_bkg, tracemodels, subarray)

        if soss_kwargs["bad_pix"] == "model":
            # Generate new trace models for each individual decontaminated orders
            bad_pix_models = tracemodels
        else:
            bad_pix_models = None

        # Use the bad pixel models to perform a de-contaminated extraction.
        kwargs = {}
        kwargs["bad_pix"] = soss_kwargs["bad_pix"]
        kwargs["tracemodels"] = bad_pix_models
        result = _extract_image(data_to_extract, scierr, scimask, box_weights, **kwargs)
        fluxes, fluxerrs, npixels = result

        # Save trace models for output reference
        for order in tracemodels:
            # Initialize a list for first integration
            if i == 0:
                all_tracemodels[order] = []
            # Put NaNs to zero
            model_ord = tracemodels[order]
            model_ord = np.where(np.isfinite(model_ord), model_ord, 0.0)
            # Save as a list (convert to array at the end)
            all_tracemodels[order].append(model_ord)

        # Save box weights for output reference
        for order in box_weights:
            # Initialize a list for first integration
            if i == 0:
                all_box_weights[order] = []
            all_box_weights[order].append(box_weights[order])
        # Copy spectral data for each order into the output model.
        for order in fluxes.keys():
            table_size = len(wavelengths[order])

            out_table = np.zeros(table_size, dtype=datamodels.SpecModel().spec_table.dtype)
            out_table["WAVELENGTH"] = wavelengths[order][:table_size]
            out_table["FLUX"] = fluxes[order][:table_size]
            out_table["FLUX_ERROR"] = fluxerrs[order][:table_size]
            out_table["DQ"] = np.zeros(table_size)
            out_table["BACKGROUND"] = col_bkg[:table_size]
            out_table["NPIXELS"] = npixels[order][:table_size]

            spec = datamodels.SpecModel(spec_table=out_table)

            # Add integration number and spectral order
            spec.spectral_order = order_str_2_int[order]
            spec.int_num = i + 1  # integration number starts at 1, not 0 like python

            if order in output_spec_list:
                output_spec_list[order].append(spec)
            else:
                output_spec_list[order] = [spec]

    # Make a TSOSpecModel from the output spec list
    for order in output_spec_list:
        tso_spec = make_tso_specmodel(
            output_spec_list[order], segment=input_model.meta.exposure.segment_number
        )
        output_model.spec.append(tso_spec)

    # Update output model
    output_model.meta.soss_extract1d.width = soss_kwargs["width"]
    output_model.meta.soss_extract1d.apply_decontamination = soss_kwargs["atoca"]
    output_model.meta.soss_extract1d.tikhonov_factor = soss_kwargs["tikfac"]
    output_model.meta.soss_extract1d.oversampling = soss_kwargs["n_os"]
    output_model.meta.soss_extract1d.threshold = soss_kwargs["threshold"]
    output_model.meta.soss_extract1d.bad_pix = soss_kwargs["bad_pix"]

    # Save output references
    for order in all_tracemodels:
        # Convert from list to array
        tracemod_ord = np.array(all_tracemodels[order])
        # Save
        order_int = order_str_2_int[order]
        setattr(output_references, f"order{order_int}", tracemod_ord)

    for order in all_box_weights:
        # Convert from list to array
        box_w_ord = np.array(all_box_weights[order])
        # Save
        order_int = order_str_2_int[order]
        setattr(output_references, f"aperture{order_int}", box_w_ord)

    if pipe_utils.is_tso(input_model):
        log.info("Populating INT_TIMES keywords from input table.")
        populate_time_keywords(input_model, output_model)
        output_model.int_times = input_model.int_times.copy()

    if soss_kwargs["wave_grid_out"] is not None:
        wave_grid_model = SossWaveGridModel(wavegrid=wave_grid)
        log.info(f"Saving soss_wave_grid to {soss_kwargs['wave_grid_out']}")
        wave_grid_model.save(path=soss_kwargs["wave_grid_out"])
        wave_grid_model.close()

    return output_model, output_references, output_atoca
