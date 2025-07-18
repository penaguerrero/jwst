"""Test input directory usage and input defaults"""

from os import path

import pytest
from astropy.utils.data import get_pkg_data_filename, get_pkg_data_path
from stdatamodels.jwst import datamodels
from stdatamodels.jwst.datamodels import JwstDataModel

from jwst.datamodels import ModelContainer
from jwst.stpipe import Step
from jwst.stpipe.tests.steps import StepWithContainer, StepWithModel


def test_default_input_with_container(tmp_cwd):
    """Test default input name from a ModelContainer"""

    model_path = get_pkg_data_filename("data/flat.fits", package="jwst.stpipe.tests")
    with ModelContainer([model_path]) as container:
        step = StepWithContainer()
        step.run(container)

        assert step._input_filename is None


def test_default_input_with_full_model():
    """Test default input name retrieval with actual model"""
    model_path = get_pkg_data_filename("data/flat.fits", package="jwst.stpipe.tests")
    with datamodels.open(model_path) as model:
        step = StepWithModel()
        step.run(model)

        assert step._input_filename == model.meta.filename


def test_default_input_with_new_model():
    """Test getting input name with new model"""

    step = StepWithModel()

    model = JwstDataModel()
    step.run(model)

    assert step._input_filename is None


def test_default_input_dir(tmp_cwd):
    """Test defaults"""
    input_file = get_pkg_data_filename("data/flat.fits", package="jwst.stpipe.tests")

    step = Step.from_cmdline(["jwst.stpipe.tests.steps.StepWithModel", input_file])

    # Check that `input_dir` is set.
    input_path = path.split(input_file)[0]
    assert step.input_dir == input_path


def test_set_input_dir(tmp_cwd):
    """Simply set the path"""
    input_file = get_pkg_data_filename("data/flat.fits", package="jwst.stpipe.tests")

    step = Step.from_cmdline(
        ["jwst.stpipe.tests.steps.StepWithModel", input_file, "--input_dir", "junkdir"]
    )

    # Check that `input_dir` is set.
    assert step.input_dir == "junkdir"


def test_use_input_dir(tmp_cwd):
    """Test with a specified path"""
    input_dir = get_pkg_data_path("data", package="jwst.stpipe.tests")
    input_file = "flat.fits"

    step = Step.from_cmdline(
        ["jwst.stpipe.tests.steps.StepWithModel", input_file, "--input_dir", input_dir]
    )

    # Check that `input_dir` is set.
    assert step.input_dir == input_dir


def test_fail_input_dir(tmp_cwd):
    """Fail with a bad file path"""
    input_file = "flat.fits"

    with pytest.raises(FileNotFoundError):
        Step.from_cmdline(
            [
                "jwst.stpipe.tests.steps.StepWithModel",
                input_file,
            ]
        )


def test_input_dir_with_model(tmp_cwd):
    """Use with an already opened DataModel"""
    with datamodels.open(
        get_pkg_data_filename("data/flat.fits", package="jwst.stpipe.tests")
    ) as model:
        step = StepWithModel()
        step.run(model)

        assert step.input_dir == ""
