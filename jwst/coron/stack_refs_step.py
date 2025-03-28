from stdatamodels.jwst import datamodels

from ..stpipe import Step

from . import stack_refs

__all__ = ["StackRefsStep"]


class StackRefsStep(Step):

    """
    StackRefsStep: Stack multiple PSF reference exposures into a
    single CubeModel, for use by subsequent coronagraphic steps.
    """

    class_alias = "stack_refs"

    spec = """
    """ # noqa: E501

    def process(self, input):

        # Open the inputs
        with datamodels.open(input) as input_models:

            # Call the stacking routine
            output_model = stack_refs.make_cube(input_models)

            output_model.meta.cal_step.stack_psfs = 'COMPLETE'

        return output_model
