from collections.abc import Iterable
from functools import reduce

import networkx as nx
from tensorflow import keras
from tensorflow.python.keras.utils.vis_utils import model_to_dot

from deephyper.core.exceptions.nas.struct import (InputShapeOfWrongType,
                                                  NodeAlreadyAdded,
                                                  StructureHasACycle,
                                                  WrongSequenceToSetOperations)
from deephyper.search.nas.model.space.node import (ConstantNode, Node,
                                                   VariableNode)
from deephyper.search.nas.model.space.op.basic import Connect, Tensor
from deephyper.search.nas.model.space.op.merge import Concatenate
from deephyper.search.nas.model.space.op.op1d import Identity
from deephyper.search.nas.model.space.struct import DirectStructure


class AutoOutputStructure(DirectStructure):
    """A DirectStructure represents a search space of neural networks.

    Args:
        input_shape (list(tuple(int))): list of shapes of all inputs.
        output_shape (tuple(int)): shape of output.

    Raises:
        InputShapeOfWrongType: [description]
    """

    def create_model(self, activation=None):
        """Create the tensors corresponding to the structure.

        Args:
            train (bool): True if the network is built for training, False if the network is built for validation/testing (for example False will deactivate Dropout).

        Returns:
            The output tensor.
        """

        output_tensor = self.create_tensor_aux(self.graph, self.output_node)
        if len(output_tensor.get_shape()) > 2:
            output_tensor = keras.layers.Flatten()(output_tensor)
        output_tensor = keras.layers.Dense(
            self.output_shape[0], activation=activation)(output_tensor)

        input_tensors = [inode._tensor for inode in self.input_nodes]

        self._model = keras.Model(inputs=input_tensors, outputs=output_tensor)

        return keras.Model(inputs=input_tensors, outputs=output_tensor)