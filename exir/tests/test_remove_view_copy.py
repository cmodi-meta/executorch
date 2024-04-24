# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import copy
import unittest

import torch
import torch.nn as nn
from executorch.exir import memory, to_edge
from executorch.exir.capture._config import ExecutorchBackendConfig
from executorch.exir.passes import MemoryPlanningPass


class TestModel1(nn.Module):
    def __init__(self):
        super().__init__()
        self.parameter = nn.Parameter(torch.rand(5, 6))
        self.parameter.requires_grad = False

    def forward(self, x):
        v1 = self.parameter.view(
            6, 5
        )  # removed, lifetime of parameter will be extended
        v2 = x.view(6, 5)  # not removed
        v3 = torch.ops.aten.mul.Tensor(v1, v2).view(
            30
        )  # removed, lifetime of mul.Tensor will be extended
        return v3

    def get_example_inputs(self):
        return (torch.rand(5, 6),)


class TestRemoveViewCopy(unittest.TestCase):
    def test_disable(self) -> None:
        model = TestModel1()
        model.eval()
        example_inputs = model.get_example_inputs()
        ep = torch.export.export(model, example_inputs)
        etpm = to_edge(ep).to_executorch(
            config=ExecutorchBackendConfig(
                remove_view_copy=False,
                memory_planning_pass=MemoryPlanningPass(
                    "greedy", alloc_graph_input=False
                ),
            ),
        )

        for node in etpm.exported_program().graph_module.graph.nodes:
            assert node.target != memory.view

    def test_output_matches(self) -> None:
        model = TestModel1()
        model.eval()
        example_inputs = model.get_example_inputs()
        ep = torch.export.export(model, example_inputs)

        epm_remove = to_edge(ep)
        epm_no_remove = copy.deepcopy(
            epm_remove
        )  # to_executorch modifies the edge_program, so we make a copy

        # Run pass with no removal
        etpm_remove = epm_remove.to_executorch(
            config=ExecutorchBackendConfig(
                remove_view_copy=True,
                memory_planning_pass=MemoryPlanningPass(
                    "greedy", alloc_graph_input=False
                ),
            ),
        )

        # Run pass with removal
        etpm_no_remove = epm_no_remove.to_executorch(
            config=ExecutorchBackendConfig(
                remove_view_copy=True,
                memory_planning_pass=MemoryPlanningPass(
                    "greedy", alloc_graph_input=False
                ),
            ),
        )

        out_remove = etpm_remove.exported_program().module()(*example_inputs)
        out_no_remove = etpm_no_remove.exported_program().module()(*example_inputs)

        self.assertTrue(torch.allclose(out_remove, out_no_remove))

    def test_spec(self) -> None:
        model = TestModel1()
        model.eval()
        example_inputs = model.get_example_inputs()
        ep = torch.export.export(model, example_inputs)

        etpm = to_edge(ep).to_executorch(
            config=ExecutorchBackendConfig(
                remove_view_copy=True,
                memory_planning_pass=MemoryPlanningPass(
                    "greedy", alloc_graph_input=False
                ),
            ),
        )

        # etpm.exported_program().graph.print_tabular()

        # idx  opcode         name                      target                              args                                                kwargs
        # ---  -------------  ------------------------  ----------------------------------  --------------------------------------------------  --------------
        # 0    placeholder    p_parameter               p_parameter                         ()                                                  {}
        # 1    placeholder    x                         x                                   ()                                                  {}
        # 2    call_function  aten_view_copy_default    <function view at 0x7facb871fb50>   (p_parameter, [6, 5])                               {}
        # 3    call_function  aten_view_copy_default_1  <function view at 0x7facb871fb50>   (x, [6, 5])                                         {}
        # 4    call_function  alloc                     <function alloc at 0x7facb871fa30>  (((6, 5), torch.float32),)                          {}
        # 5    call_function  aten_mul_tensor           aten.mul.out                        (aten_view_copy_default, aten_view_copy_default_1)  {'out': alloc}
        # 6    call_function  aten_view_copy_default_2  <function view at 0x7facb871fb50>   (aten_mul_tensor, [30])                             {}
        # 7    output         output_1                  output                              ((aten_view_copy_default_2,),)                      {}

        for node in etpm.exported_program().graph.nodes:
            if node.name == "p_parameter":
                # p_parameter's lifetime is extended through aten_view_copy_default (memory.view) to idx 5
                self.assertEqual(node.meta["spec"].lifetime, [0, 5])
            elif node.name == "aten_view_copy_default":
                # aten_view_copy_default is a memory.view of p_parameter.
                # p_parameter is a constant with storage, so we check that the view's storage matches the base

                # assert base is p_parameter
                self.assertEqual(node.args[0].name, "p_parameter")

                # assert base is const with storage
                self.assertTrue(node.args[0].meta["spec"].const)
                self.assertTrue(node.args[0].meta["spec"].storage is not None)
                self.assertTrue(node.args[0].meta["spec"].mem_id is None)
                self.assertTrue(node.args[0].meta["spec"].mem_offset is None)

                # assert self is const with storage
                self.assertTrue(node.meta["spec"].const)
                self.assertTrue(node.meta["spec"].storage is not None)
                self.assertTrue(node.meta["spec"].mem_id is None)
                self.assertTrue(node.meta["spec"].mem_offset is None)

                # assert storage matches
                self.assertEqual(
                    node.meta["spec"].storage, node.args[0].meta["spec"].storage
                )

                # assert lifetime matches
                self.assertEqual(
                    node.meta["spec"].lifetime, node.args[0].meta["spec"].lifetime
                )
            elif node.name == "aten_mul_tensor":
                # aten_mul_tensor's lifetime is extended through aten_view_copy_default_2 (memory.view) to idx 7
                self.assertEqual(node.meta["spec"].lifetime, [4, 7])
            elif node.name == "aten_view_copy_default_2":
                # aten_view_copy_default_2 is a memory.view of aten_mul_tensor

                # assert base is aten_mul_tensor
                self.assertEqual(node.args[0].name, "aten_mul_tensor")

                # assert base and self are not const, do not have storage,
                # but do have mem_id and mem_offset
                self.assertFalse(node.args[0].meta["spec"].const)
                self.assertTrue(node.args[0].meta["spec"].storage is None)
                self.assertTrue(node.args[0].meta["spec"].mem_id is not None)
                self.assertTrue(node.args[0].meta["spec"].mem_offset is not None)

                self.assertFalse(node.meta["spec"].const)
                self.assertTrue(node.meta["spec"].storage is None)
                self.assertTrue(node.meta["spec"].mem_id is not None)
                self.assertTrue(node.meta["spec"].mem_offset is not None)

                # assert self and base mem_id, mem_offset, and lifetime matches
                self.assertEqual(
                    node.meta["spec"].mem_id, node.args[0].meta["spec"].mem_id
                )
                self.assertEqual(
                    node.meta["spec"].mem_offset, node.args[0].meta["spec"].mem_offset
                )
                self.assertEqual(
                    node.meta["spec"].lifetime, node.args[0].meta["spec"].lifetime
                )

        # Test evalues in execution plan
        plan = etpm.executorch_program.execution_plan[0]
        self.assertEqual(plan.operators[0].name, "executorch_prim::et_view")
        self.assertEqual(plan.operators[1].name, "aten::mul")

        instructions = plan.chains[0].instructions
        self.assertEqual(len(instructions), 4)

        self.assertEqual(
            instructions[0].instr_args.op_index, 0  # pyre-ignore
        )  # view @ idx2
        self.assertEqual(
            instructions[1].instr_args.op_index, 0  # pyre-ignore
        )  # view @ idx3
        self.assertEqual(
            instructions[2].instr_args.op_index, 1  # pyre-ignore
        )  # aten:mul @ idx5
        self.assertEqual(
            instructions[3].instr_args.op_index, 0  # pyre-ignore
        )  # view @ idx6