import warnings
from collections import deque, defaultdict
from typing import List, Dict, Tuple, Iterable, Union, Optional

import numpy as np
import torch
import torch.nn as nn
from torch import Tensor

from ..modules.base import InvertibleModule


class Node:
    """
    The Node class represents one transformation in the graph, with an
    arbitrary number of in- and outputs.

    The user specifies the input, and the underlying module computes the
    number of outputs.
    """

    def __init__(self, inputs: Union["Node", Tuple["Node", int],
                                     Iterable[Tuple["Node", int]]],
                 module_type, module_args: dict, conditions=None, name=None):
        if conditions is None:
            conditions = []

        if name:
            self.name = name
        else:
            self.name = hex(id(self))[-6:]
        for i in range(256):
            exec('self.out{0} = (self, {0})'.format(i))

        self.inputs = self.parse_inputs(inputs)
        if isinstance(conditions, (list, tuple)):
            self.conditions = conditions
        else:
            self.conditions = [conditions, ]

        self.outputs: List[Tuple[Node, int]] = []
        self.module_type = module_type
        self.module_args = module_args

        input_shapes = [input_node.output_dims[node_out_idx]
                        for input_node, node_out_idx in self.inputs]
        condition_shapes = [cond_node.output_dims[0]
                            for cond_node in self.conditions]

        self.input_dims = input_shapes
        self.condition_dims = condition_shapes
        self.module, self.output_dims = self.build_module(condition_shapes,
                                                          input_shapes)

        # Notify preceding nodes that their output ends up here
        # Entry at position co -> (n, ci) means:
        # My output co goes to input channel ci of n.
        for in_idx, (in_node, out_idx) in enumerate(self.inputs):
            in_node.outputs.append((self, in_idx))

    def __getattr__(self, item):
        if item.startswith("out"):
            return self, int(item[3:])
        raise AttributeError(item)

    def build_module(self, condition_shapes, input_shapes) \
            -> Tuple[InvertibleModule, List[Tuple[int]]]:
        """
        Instantiates the module and determines the output dimension by
        calling InvertibleModule#output_dims.
        """
        if len(self.conditions) > 0:
            module = self.module_type(input_shapes, dims_c=condition_shapes,
                                      **self.module_args)
        else:
            module = self.module_type(input_shapes, **self.module_args)
        return module, module.output_dims(input_shapes)

    def parse_inputs(self, inputs: Union["Node", Tuple["Node", int],
                                         Iterable[Tuple["Node", int]]]) \
            -> List[Tuple["Node", int]]:
        """
        Converts specified inputs to a node to a canonical format.
        Inputs can be specified in three forms:

        - a single node, then this nodes first output is taken as input
        - a single tuple (node, idx), specifying output idx of node
        - a list of tuples [(node, idx)], each specifying output idx of node

        All such formats are converted to the last format.
        """
        if isinstance(inputs, (list, tuple)):
            if len(inputs) == 0:
                return inputs
            elif isinstance(inputs[0], (list, tuple)):
                return inputs
            elif len(inputs) == 2:
                return [inputs, ]
            else:
                raise RuntimeError(
                    f"Cannot parse inputs provided to node '{self.name}'.")
        else:
            assert isinstance(inputs, Node), "Received object of invalid " \
                                             "type ({type(inputs)}) as input " \
                                             "for node '{self.name}'."
            return [(inputs, 0), ]

    def __str__(self):
        module_name = (self.module_type.__name__ if self.module_type is not None
                       else "")
        return f"{self.__class__.__name__}({self.input_dims} -> " \
               f"{module_name} -> {self.output_dims})"

    def __repr__(self):
        return str(self)


class InputNode(Node):
    """
    Special type of node that represents the input data of the whole net (or the
    output when running reverse)
    """

    def __init__(self, *dims: int, name=None):
        self.dims = dims
        super().__init__([], None, {}, name=name)

    def build_module(self, condition_shapes, input_shapes) \
            -> Tuple[None, List[Tuple[int]]]:
        if len(condition_shapes) > 0:
            raise ValueError(
                f"{self.__class__.__name__} does not accept conditions")
        assert len(input_shapes) == 0, "Forbidden by constructor"
        return None, [self.dims]


class ConditionNode(Node):
    """
    Special type of node that represents contitional input to the internal
    networks inside coupling layers.
    """

    def __init__(self, *dims: int, name=None):
        self.dims = dims
        super().__init__([], None, {}, name=name)

    def build_module(self, condition_shapes, input_shapes) \
            -> Tuple[None, List[Tuple[int]]]:
        if len(condition_shapes) > 0:
            raise ValueError(
                f"{self.__class__.__name__} does not accept conditions")
        assert len(input_shapes) == 0, "Forbidden by constructor"
        return None, [self.dims]


class OutputNode(Node):
    """
    Special type of node that represents the output of the whole net (or the
    input when running in reverse).
    """

    def __init__(self, inputs, name=None):
        super().__init__(inputs, None, {}, name=name)

    def build_module(self, condition_shapes, input_shapes) \
            -> Tuple[None, List[Tuple[int]]]:
        if len(condition_shapes) > 0:
            raise ValueError(
                f"{self.__class__.__name__} does not accept conditions")
        return None, []


class ReversibleGraphNet(InvertibleModule):
    """
    This class represents the invertible net itself. It is a subclass of
    InvertibleModule and supports the same methods.

    The forward method has an additional option 'rev', with which the net can be
    computed in reverse. Passing `jac` to the forward method additionally
    computes the log determinant of the (inverse) Jacobian of the forward
    (backward) pass.
    """

    def __init__(self, node_list, ind_in=None, ind_out=None, verbose=True,
                 force_tuple_output=False):
        # Gather lists of input, output and condition nodes
        if ind_in is not None:
            warnings.warn(
                "Use of 'ind_in' and 'ind_out' for ReversibleGraphNet is "
                "deprecated, input and output nodes are detected "
                "automatically.")
            if isinstance(ind_in, int):
                ind_in = [ind_in]

            in_nodes = [node_list[i] for i in ind_in]
        else:
            in_nodes = [node_list[i] for i in range(len(node_list))
                        if isinstance(node_list[i], InputNode)]
        assert len(in_nodes) > 0, "No input nodes specified."

        if ind_out is not None:
            warnings.warn(
                "Use of 'ind_in' and 'ind_out' for ReversibleGraphNet is "
                "deprecated, input and output nodes are detected "
                "automatically.")
            if isinstance(ind_out, int):
                ind_out = [ind_out]

            out_nodes = [node_list[i] for i in ind_out]
        else:
            out_nodes = [node_list[i] for i in range(len(node_list))
                         if isinstance(node_list[i], OutputNode)]
        assert len(out_nodes) > 0, "No output nodes specified."

        condition_nodes = [node_list[i] for i in range(len(node_list)) if
                           isinstance(node_list[i], ConditionNode)]

        # Build the graph and tell nodes about their dimensions so that they can
        # build the modules
        node_list = topological_order(in_nodes, node_list, out_nodes)
        global_in_shapes = [node.output_dims[0] for node in in_nodes]
        global_out_shapes = [node.input_dims[0] for node in out_nodes]
        global_cond_shapes = [node.input_dims[0] for node in condition_nodes]

        # Only now we can set out shapes
        super().__init__(global_in_shapes, global_cond_shapes)
        self.global_out_shapes = global_out_shapes

        # Now we can store everything -- before calling super constructor,
        # nn.Module doesn't allow assigning anything
        self.in_nodes = in_nodes
        self.condition_nodes = condition_nodes
        self.out_nodes = out_nodes

        self.force_tuple_output = force_tuple_output
        self.module_list = nn.ModuleList([n.module for n in node_list])

    def output_dims(self, input_dims: List[Tuple[int]]) -> List[Tuple[int]]:
        return self.global_out_shapes

    def forward(self, x_or_z: Union[Tensor, Iterable[Tensor]],
                c: Iterable[Tensor], rev: bool = False, jac: bool = True,
                intermediate_outputs: bool = False)\
            -> Tuple[Tuple[Tensor], Tensor]:
        """
        Forward or backward computation of the whole net.
        """
        jacobian = None
        outs = {}
        for tensor, start_node in zip(x_or_z,
                                      self.out_nodes if rev else self.in_nodes):
            outs[start_node, 0] = tensor
        for tensor, condition_node in zip(c, self.condition_nodes):
            outs[condition_node, 0] = tensor

        # Go backwards through nodes if rev=True
        for node in self.node_list[::-1 if rev else 1]:
            has_condition = len(node.conditions) > 0

            mod_in = []
            mod_c = []
            for prev_node, channel in (node.outputs if rev else node.inputs):
                mod_in.append(outs[prev_node, channel])
            for cond_node in node.conditions:
                mod_c.append(outs[cond_node, 0])
            mod_in = tuple(mod_in)
            mod_c = tuple(mod_c)

            if has_condition:
                mod_out = node.module(mod_in, c=mod_c, rev=rev, jac=jac)
            else:
                mod_out = node.module(mod_in, rev=rev, jac=jac)

            if torch.is_tensor(mod_out):
                raise ValueError(
                    f"The node {node}'s module returned a tensor only. This "
                    f"is deprecated without fallback. Please follow the "
                    f"signature of InvertibleOperator#forward in your module "
                    f"if you want to use it in a ReversibleGraphNet.")
            if len(mod_out) != 2:
                raise ValueError(
                    f"The node {node}'s module returned a tuple of length "
                    f"{len(mod_out)}, but should return a tuple `z_or_x, jac`.")

            out, mod_jac = mod_out

            if torch.is_tensor(out):
                # Not according to specification!
                if isinstance(node.module, ReversibleGraphNet):
                    add_text = (" Consider passing force_tuple_output=True to"
                                " the contained ReversibleGraphNet")
                else:
                    add_text = ""
                raise ValueError(
                    f"The node {node}'s module returns a tensor.{add_text}")
            if len(out) != len(node.inputs if rev else node.outputs):
                raise ValueError(
                    f"The node {node}'s module returned {len(out)} output "
                    f"variables, but should return "
                    f"{len(node.inputs if rev else node.outputs)}.")
            if not torch.is_tensor(mod_jac):
                if jac:
                    raise ValueError(
                        f"The node {node}'s module returned a non-tensor as "
                        f"Jacobian.")
                elif not jac and mod_jac is not None:
                    raise ValueError(
                        f"The node {node}'s module returned neither None nor a "
                        f"Jacobian.")

            for out_idx, out_value in enumerate(out):
                outs[self, out_idx] = out_value

            if jac:
                jacobian = jacobian + mod_jac

        if intermediate_outputs:
            return outs, jacobian
        else:
            out_list = [outs[(out_node, 0)] for out_node in self.out_nodes]
            if len(out_list) == 1 and not self.force_tuple_output:
                return out_list[0], jacobian
            else:
                return tuple(out_list), jacobian

    def log_jacobian_numerical(self, x, c=None, rev=False, h=1e-04):
        """
        Approximate log Jacobian determinant via finite differences.
        """
        if isinstance(x, (list, tuple)):
            batch_size = x[0].shape[0]
            ndim_x_separate = [np.prod(x_i.shape[1:]) for x_i in x]
            ndim_x_total = sum(ndim_x_separate)
            x_flat = torch.cat([x_i.view(batch_size, -1) for x_i in x], dim=1)
        else:
            batch_size = x.shape[0]
            ndim_x_total = np.prod(x.shape[1:])
            x_flat = x.reshape(batch_size, -1)

        J_num = torch.zeros(batch_size, ndim_x_total, ndim_x_total)
        for i in range(ndim_x_total):
            offset = x[0].new_zeros(batch_size, ndim_x_total)
            offset[:, i] = h
            if isinstance(x, (list, tuple)):
                x_upper = torch.split(x_flat + offset, ndim_x_separate, dim=1)
                x_upper = [x_upper[i].view(*x[i].shape) for i in range(len(x))]
                x_lower = torch.split(x_flat - offset, ndim_x_separate, dim=1)
                x_lower = [x_lower[i].view(*x[i].shape) for i in range(len(x))]
            else:
                x_upper = (x_flat + offset).view(*x.shape)
                x_lower = (x_flat - offset).view(*x.shape)
            y_upper = self.forward(x_upper, c=c, rev=rev, jac=False)
            y_lower = self.forward(x_lower, c=c, rev=rev, jac=False)
            if isinstance(y_upper, (list, tuple)):
                y_upper = torch.cat(
                    [y_i.view(batch_size, -1) for y_i in y_upper], dim=1)
                y_lower = torch.cat(
                    [y_i.view(batch_size, -1) for y_i in y_lower], dim=1)
            J_num[:, :, i] = (y_upper - y_lower).view(batch_size, -1) / (2 * h)
        logdet_num = x[0].new_zeros(batch_size)
        for i in range(batch_size):
            logdet_num[i] = torch.det(J_num[i, :, :]).abs().log()

        return logdet_num

    def get_node_by_name(self, name) -> Optional[Node]:
        """
        Return the first node in the graph with the provided name.
        """
        for node in self.node_list:
            if node.name == name:
                return node
        return None

    def get_module_by_name(self, name) -> Optional[nn.Module]:
        """
        Return module of the first node in the graph with the provided name.
        """
        node = self.get_node_by_name(name)
        try:
            return node.module
        except AttributeError:
            return None


def topological_order(all_nodes: List[Node], in_nodes: List[InputNode],
                      out_nodes: List[OutputNode]) -> List[Node]:
    """
    Computes the topological order of nodes.

    Parameters:
        all_nodes: All nodes in the computation graph.
        in_nodes: Input nodes (must also be present in `all_nodes`)
        out_nodes: Output nodes (must also be present in `all_nodes`)

    Returns:
        A sorted list of nodes, where the inputs to some node in the list
        are available when all previous nodes in the list have been executed.
    """
    # Edge dicts in both directions
    edges_out_to_in = {node_b: {node_a for node_a, out_idx in node_b.inputs} for
                       node_b in all_nodes + out_nodes}
    edges_in_to_out = defaultdict(set)
    for node_out, node_ins in edges_out_to_in.items():
        for node_in in node_ins:
            edges_in_to_out[node_in].add(node_out)

    # Kahn's algorithm starting from the output nodes
    sorted_nodes = []
    no_pending_edges = deque(out_nodes)

    while len(no_pending_edges) > 0:
        node = no_pending_edges.popleft()
        sorted_nodes.append(node)
        for in_node in list(edges_out_to_in[node]):
            edges_out_to_in[node].remove(in_node)
            edges_in_to_out[in_node].remove(node)

            if len(edges_in_to_out[in_node]) == 0:
                no_pending_edges.append(in_node)

    for in_node in in_nodes:
        assert in_node in sorted_nodes, f"Error in graph: Input node " \
                                        f"{in_node} is not connected " \
                                        f"to any output."

    if sum(map(len, edges_in_to_out.values())) == 0:
        return sorted_nodes[::-1]
    else:
        raise ValueError("Graph is cyclic.")
