import numpy as np
from numba import cuda

from loader.core.quantize_cpu import CPUStateDownSampler
from nml import CPUTensor, Device

try:
    from loader.core.quantize_gpu import CUDAStateDownSampler
    from nml import GPUTensor
except ImportError:
    GPUTensor = None
from nml.tensor import Tensor


class DataManager:
    """
    Data manager for MNIST digits that handles different processing and memory modes.
    Supports loading, quantization (downsampling), and batch sampling of MNIST data.

    Returns proper tensor objects (CPUTensor, GPUTensor) instead of raw arrays.
    """

    num_classes = 10

    def __init__(
        self,
        data_path: str,
        labels_path: str,
        bit_width: int,
        batch_size: int,
        process_device: Device = Device.CPU,
        storage_device: Device = Device.CPU,
    ):
        self.data_path = data_path
        self.labels_path = labels_path
        self.bit_width = bit_width
        self.batch_size = batch_size
        self.process_device = process_device
        self.storage_device = storage_device

        self.data_cpu = None
        self.data_gpu = None
        self.labels_cpu = None

        if self.process_device == Device.CPU and self.storage_device == Device.GPU:
            raise NotImplementedError(
                "Only three modes available: 'cpu to cpu', 'gpu_to_cpu', 'gpu_to_gpu'"
            )

        if self.process_device == Device.CPU:
            self.downsampler = CPUStateDownSampler(self.bit_width)
        elif self.process_device == Device.GPU and GPUTensor is not None:
            self.downsampler = CUDAStateDownSampler(self.bit_width)
        else:
            raise NotImplementedError(
                f"Device {self.process_device} not supported for processing."
            )

        if self.storage_device == Device.GPU and GPUTensor is None:
            raise NotImplementedError(
                "GPU storage is not supported. Please install the GPU version of NML."
            )
        if self.storage_device not in {Device.CPU, Device.GPU}:
            raise NotImplementedError(
                f"Storage device {self.storage_device} not supported. "
            )

        self.all_indices = None

    def _convert_to_one(self, labels, num_classes=10):
        """Convert integer labels to [0, 0, 1, 0, 0, 0, 0, 0, 0] encoded vectors."""
        one_hot = np.zeros((labels.size, num_classes), dtype=np.uint8)
        one_hot[np.arange(labels.size), labels] = 1
        return one_hot

    def load_data(self) -> None:
        """Load MNIST data and labels from disk into CPU memory."""
        data_array = np.load(self.data_path)
        if (
            len(data_array.shape) != 3
            or data_array.shape[1:] != (28, 28)
            or data_array.dtype != np.uint8
        ):
            raise ValueError(
                f"Expected MNIST data of shape (N, 28, 28) and dtype uint8, "
                f"got shape {data_array.shape} and dtype {data_array.dtype}"
            )

        labels_array = np.load(self.labels_path)

        self.all_indices = []
        for class_label in range(self.num_classes):
            class_indices = np.where(labels_array == class_label)[0]
            self.all_indices.append(class_indices)

        one_hot_labels = self._convert_to_one(labels_array)
        self.labels_cpu = CPUTensor(one_hot_labels)
        self.data_cpu = CPUTensor(data_array)

    def downsample(self) -> None:
        """Apply quantization based on the selected processing and storage devices."""
        if self.data_cpu is None:
            raise RuntimeError("Data not loaded. Call load_data() first.")

        if self.process_device == Device.CPU:
            raw_data = self.downsampler(self.data_cpu.array)
            self.data_cpu = CPUTensor(raw_data)

            if self.storage_device == Device.GPU:
                self.data_gpu = GPUTensor(cuda.to_device(self.data_cpu.array))
                self.data_cpu = None

        elif self.process_device == Device.GPU:
            d_array = cuda.to_device(self.data_cpu.array)
            self.downsampler(d_array)

            if self.storage_device == Device.CPU:
                self.data_cpu = CPUTensor(d_array.copy_to_host())
                self.data_gpu = None
            else:
                self.data_gpu = GPUTensor(d_array)
                self.data_cpu = None

    def get_samples(self) -> tuple[Tensor, Tensor]:
        """
        Randomly select batch_size images and their labels from the quantized tensor.

        Returns:
            Tuple of (images, labels) where images is a 3D tensor (batch_size, height, width)
            and labels is a 2D tensor (batch_size, one_hot_encoding)
        """
        samples_per_class = self.batch_size // self.num_classes
        remaining = self.batch_size % self.num_classes

        selected_indices = np.empty(self.batch_size, dtype=np.int32)
        offset = 0

        for indices in self.all_indices:
            count = samples_per_class
            if remaining > 0:
                count += 1
                remaining -= 1

            selected = np.random.choice(
                indices, min(count, len(indices)), replace=False
            )
            selected_indices[offset : offset + len(selected)] = selected
            offset += len(selected)

        if offset != self.batch_size:
            raise RuntimeError(f"Expected {self.batch_size} samples, but got {offset}.")

        batch_labels = self.labels_cpu.array[selected_indices]
        batch_labels = CPUTensor(batch_labels)

        if self.storage_device == Device.CPU:
            if self.data_cpu is None:
                raise RuntimeError(
                    "CPU data not available. Ensure downsample() has been called."
                )
            return CPUTensor(self.data_cpu.array[selected_indices]), batch_labels
        else:
            if self.data_gpu is None:
                raise RuntimeError(
                    "GPU data not available. Ensure downsample() has been called."
                )
            data_cpu = self.data_gpu.array.copy_to_host()
            batch_cpu = data_cpu[selected_indices]
            return GPUTensor(cuda.to_device(batch_cpu)), batch_labels

    def __call__(self) -> tuple[Tensor, Tensor]:
        """
        Randomly select batch_size images and their labels from the quantized tensor.

        Returns:
            Tuple of (images, labels) where images is a 3D tensor (batch_size, height, width)
            and labels is a 2D tensor (batch_size, one_hot_encoding)
        """
        return self.get_samples()
