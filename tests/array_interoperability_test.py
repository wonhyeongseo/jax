# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from absl.testing import absltest, parameterized

import jax
from jax.config import config
import jax.dlpack
from jax._src.lib import xla_bridge, xla_client
import jax.numpy as jnp
from jax._src import test_util as jtu

import numpy as np

numpy_version = tuple(map(int, np.__version__.split('.')[:3]))

config.parse_flags_with_absl()

try:
  import torch
  import torch.utils.dlpack
except ImportError:
  torch = None

try:
  import cupy
except ImportError:
  cupy = None

try:
  import tensorflow as tf
  tf_version = tuple(
    int(x) for x in tf.version.VERSION.split("-")[0].split("."))
except:
  tf = None


dlpack_dtypes = sorted(list(jax.dlpack.SUPPORTED_DTYPES),
                       key=lambda x: x.__name__)
torch_dtypes = [jnp.int8, jnp.int16, jnp.int32, jnp.int64,
                jnp.uint8, jnp.float16, jnp.float32, jnp.float64]

nonempty_nonscalar_array_shapes = [(4,), (3, 4), (2, 3, 4)]
empty_array_shapes = []
empty_array_shapes += [(0,), (0, 4), (3, 0),]
nonempty_nonscalar_array_shapes += [(3, 1), (1, 4), (2, 1, 4)]

nonempty_array_shapes = [()] + nonempty_nonscalar_array_shapes
all_shapes = nonempty_array_shapes + empty_array_shapes

class DLPackTest(jtu.JaxTestCase):
  def setUp(self):
    super().setUp()
    if jtu.device_under_test() == "tpu":
      self.skipTest("DLPack not supported on TPU")

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}_take_ownership={}_gpu={}".format(
        jtu.format_shape_dtype_string(shape, dtype),
        take_ownership, gpu),
      "shape": shape, "dtype": dtype, "take_ownership": take_ownership,
      "gpu": gpu}
     for shape in all_shapes
     for dtype in dlpack_dtypes
     for take_ownership in [False, True]
     for gpu in [False, True]))
  @jtu.skip_on_devices("rocm") # TODO(sharadmv,phawkins): see GH issue #10973
  def testJaxRoundTrip(self, shape, dtype, take_ownership, gpu):
    rng = jtu.rand_default(self.rng())
    np = rng(shape, dtype)
    if gpu and jax.default_backend() == "cpu":
      raise unittest.SkipTest("Skipping GPU test case on CPU")
    device = jax.devices("gpu" if gpu else "cpu")[0]
    x = jax.device_put(np, device)
    dlpack = jax.dlpack.to_dlpack(x, take_ownership=take_ownership)
    self.assertEqual(take_ownership, x.is_deleted())
    y = jax.dlpack.from_dlpack(dlpack)
    self.assertEqual(y.device(), device)
    self.assertAllClose(np.astype(x.dtype), y)

    self.assertRaisesRegex(RuntimeError,
                           "DLPack tensor may be consumed at most once",
                           lambda: jax.dlpack.from_dlpack(dlpack))

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in dlpack_dtypes))
  @unittest.skipIf(not tf, "Test requires TensorFlow")
  @jtu.skip_on_devices("rocm") # TODO(sharadmv,phawkins): see GH issue #10973
  def testTensorFlowToJax(self, shape, dtype):
    if not config.x64_enabled and dtype in [jnp.int64, jnp.uint64, jnp.float64]:
      raise self.skipTest("x64 types are disabled by jax_enable_x64")
    if (jtu.device_under_test() == "gpu" and
        not tf.config.list_physical_devices("GPU")):
      raise self.skipTest("TensorFlow not configured with GPU support")

    if jtu.device_under_test() == "gpu" and dtype == jnp.int32:
      raise self.skipTest("TensorFlow does not place int32 tensors on GPU")

    rng = jtu.rand_default(self.rng())
    np = rng(shape, dtype)
    with tf.device("/GPU:0" if jtu.device_under_test() == "gpu" else "/CPU:0"):
      x = tf.identity(tf.constant(np))
    dlpack = tf.experimental.dlpack.to_dlpack(x)
    y = jax.dlpack.from_dlpack(dlpack)
    self.assertAllClose(np, y)

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in dlpack_dtypes))
  @unittest.skipIf(not tf, "Test requires TensorFlow")
  def testJaxToTensorFlow(self, shape, dtype):
    if not config.x64_enabled and dtype in [jnp.int64, jnp.uint64,
                                              jnp.float64]:
      self.skipTest("x64 types are disabled by jax_enable_x64")
    if (jtu.device_under_test() == "gpu" and
        not tf.config.list_physical_devices("GPU")):
      raise self.skipTest("TensorFlow not configured with GPU support")
    rng = jtu.rand_default(self.rng())
    np = rng(shape, dtype)
    x = jnp.array(np)
    # TODO(b/171320191): this line works around a missing context initialization
    # bug in TensorFlow.
    _ = tf.add(1, 1)
    dlpack = jax.dlpack.to_dlpack(x)
    y = tf.experimental.dlpack.from_dlpack(dlpack)
    self.assertAllClose(np, y.numpy())

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in torch_dtypes))
  @unittest.skipIf(not torch, "Test requires PyTorch")
  def testTorchToJax(self, shape, dtype):
    if not config.x64_enabled and dtype in [jnp.int64, jnp.float64]:
      self.skipTest("x64 types are disabled by jax_enable_x64")
    rng = jtu.rand_default(self.rng())
    np = rng(shape, dtype)
    x = torch.from_numpy(np)
    x = x.cuda() if jtu.device_under_test() == "gpu" else x
    dlpack = torch.utils.dlpack.to_dlpack(x)
    y = jax.dlpack.from_dlpack(dlpack)
    self.assertAllClose(np, y)

  @unittest.skipIf(not torch, "Test requires PyTorch")
  def testTorchToJaxFailure(self):
    x = torch.arange(6).reshape((2, 3))
    y = torch.utils.dlpack.to_dlpack(x[:, :2])

    backend = xla_bridge.get_backend()
    client = getattr(backend, "client", backend)

    regex_str = (r'UNIMPLEMENTED: Only DLPack tensors with trivial \(compact\) '
                 r'striding are supported')
    with self.assertRaisesRegex(RuntimeError, regex_str):
      xla_client._xla.dlpack_managed_tensor_to_buffer(
          y, client)

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in torch_dtypes))
  @unittest.skipIf(not torch, "Test requires PyTorch")
  def testJaxToTorch(self, shape, dtype):
    if not config.x64_enabled and dtype in [jnp.int64, jnp.float64]:
      self.skipTest("x64 types are disabled by jax_enable_x64")
    rng = jtu.rand_default(self.rng())
    np = rng(shape, dtype)
    x = jnp.array(np)
    dlpack = jax.dlpack.to_dlpack(x)
    y = torch.utils.dlpack.from_dlpack(dlpack)
    self.assertAllClose(np, y.cpu().numpy())

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in torch_dtypes))
  @unittest.skipIf(numpy_version < (1, 22, 0), "Requires numpy 1.22 or newer")
  @jtu.skip_on_devices("rocm") # TODO(sharadmv,phawkins): see GH issue #10973
  def testNumpyToJax(self, shape, dtype):
    rng = jtu.rand_default(self.rng())
    x_np = rng(shape, dtype)
    x_jax = jnp.from_dlpack(x_np)
    self.assertAllClose(x_np, x_jax)

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in torch_dtypes))
  @unittest.skipIf(numpy_version < (1, 22, 0), "Requires numpy 1.22 or newer")
  @jtu.skip_on_devices("gpu")
  def testJaxToNumpy(self, shape, dtype):
    rng = jtu.rand_default(self.rng())
    x_jax = jnp.array(rng(shape, dtype))
    x_np = np.from_dlpack(x_jax)
    self.assertAllClose(x_np, x_jax)


class CudaArrayInterfaceTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "gpu":
      self.skipTest("__cuda_array_interface__ is only supported on GPU")

  @parameterized.named_parameters(jtu.cases_from_list(
     {"testcase_name": "_{}".format(
        jtu.format_shape_dtype_string(shape, dtype)),
     "shape": shape, "dtype": dtype}
     for shape in all_shapes
     for dtype in dlpack_dtypes))
  @unittest.skipIf(not cupy, "Test requires CuPy")
  def testJaxToCuPy(self, shape, dtype):
    if dtype == jnp.bfloat16:
      raise unittest.SkipTest("cupy does not support bfloat16")
    rng = jtu.rand_default(self.rng())
    x = rng(shape, dtype)
    y = jnp.array(x)
    z = cupy.asarray(y)
    self.assertEqual(y.__cuda_array_interface__["data"][0],
                     z.__cuda_array_interface__["data"][0])
    self.assertAllClose(x, cupy.asnumpy(z))


class Bfloat16Test(jtu.JaxTestCase):

  @unittest.skipIf((not tf or tf_version < (2, 5, 0)),
                   "Test requires TensorFlow 2.5.0 or newer")
  def testJaxAndTfHaveTheSameBfloat16Type(self):
    self.assertEqual(np.dtype(jnp.bfloat16).num,
                     np.dtype(tf.dtypes.bfloat16.as_numpy_dtype).num)


if __name__ == "__main__":
  absltest.main(testLoader=jtu.JaxTestLoader())
