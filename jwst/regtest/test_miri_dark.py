import pytest

from jwst.regtest.st_fitsdiff import STFITSDiff as FITSDiff
from jwst.stpipe import Step


@pytest.mark.bigdata
@pytest.mark.parametrize(
    "exposure",
    [
        "jw04482014001_02102_00001_mirifulong",
        "jw04482014001_02102_00001_mirifushort",
        "jw04482014001_02102_00001_mirimage",
    ],
)
def test_miri_dark_pipeline(exposure, rtdata, fitsdiff_default_kwargs):
    """Test the DarkPipeline on MIRI dark exposures"""
    rtdata.get_data(f"miri/image/{exposure}_uncal.fits")

    args = ["jwst.pipeline.DarkPipeline", rtdata.input]
    Step.from_cmdline(args)
    rtdata.output = f"{exposure}_dark.fits"

    rtdata.get_truth(f"truth/test_miri_dark_pipeline/{exposure}_dark.fits")

    diff = FITSDiff(rtdata.output, rtdata.truth, **fitsdiff_default_kwargs)
    assert diff.identical, diff.report()


@pytest.mark.bigdata
@pytest.mark.parametrize("exposure", ["jw01033005001_04103_00001-seg003_mirimage"])
def test_miri_segmented_dark(exposure, rtdata, fitsdiff_default_kwargs):
    """Test the dark_current step on MIRI segmented exposures"""
    rtdata.get_data(f"miri/image/{exposure}_linearity.fits")

    args = ["jwst.dark_current.DarkCurrentStep", rtdata.input]
    Step.from_cmdline(args)
    rtdata.output = f"{exposure}_darkcurrentstep.fits"

    rtdata.get_truth(f"truth/test_miri_segmented_dark/{exposure}_darkcurrentstep.fits")

    diff = FITSDiff(rtdata.output, rtdata.truth, **fitsdiff_default_kwargs)
    assert diff.identical, diff.report()
