import logging

import numpy as np

log = logging.getLogger(__name__)

__all__ = ["subtract"]


def subtract(model1, model2):
    """
    Subtract one data model from another, and include updated DQ in output.

    Parameters
    ----------
    model1 : ImageModel or IFUImageModel
        Input data model on which subtraction will be performed

    model2 : ImageModel or IFUImageModel
        Input data model that will be subtracted from the first model

    Returns
    -------
    output : ImageModel or IFUImageModel
        Subtracted data model
    """
    # Create the output model as a copy of the first input
    output = model1.copy()

    # Subtract the SCI arrays
    output.data = model1.data - model2.data

    # Combine the ERR arrays in quadrature
    # NOTE: currently stubbed out until ERR handling is decided
    # output.err = np.sqrt(model1.err**2 + model2.err**2)

    # Combine the DQ flag arrays using bitwise OR
    output.dq = np.bitwise_or(model1.dq, model2.dq)

    # Return the subtracted model
    return output
