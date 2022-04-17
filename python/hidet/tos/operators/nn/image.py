from typing import Union, Sequence, Optional, List
from ..common import Task, Operator, Tensor, DataLayout, TensorInput, Grid, tensor_input, compute, reduce, inline_compute, tensor_type, input_like
from hidet.ir.expr import Expr, if_then_else, convert, cast, And
from hidet.ir.primitives import cuda_round, cuda_floor, cuda_ceil, cuda_max, cuda_min

# Acknowledgement: take TVM resize topi implementation as a reference


def get_origin_index(x: Expr, image_width: int, target_width: int,  coordinate_transformation_mode: str) -> Expr:
    scale = image_width / target_width
    func_map = {
        'half_pixel':
            lambda x: (x + 0.5) * scale - 0.5,
        'align_corners':
            lambda x: x * ((image_width - 1) / (target_width - 1)),
        'asymmetric':
            lambda x: x * scale,
        'pytorch_half_pixel':
            lambda x: (x + 0.5) * scale if target_width > 1 else convert(0.0),
        'tf_half_pixel_for_nn':
            lambda x: (x + 0.5) * scale
    }
    if coordinate_transformation_mode not in func_map:
        raise ValueError('Unsupported coordinate transformation mode: {}, candidates: {}.'.format(
            coordinate_transformation_mode, func_map.keys()
        ))
    return func_map[coordinate_transformation_mode](x)


def get_closest_index(x: Expr, rounding_method: str) -> Expr:
    func_map = {
        'rounding_method':
            lambda x: cast(cuda_round(x), 'int32'),
        'round_prefer_floor':
            lambda x: cast(cuda_ceil(x - 0.5), 'int32'),
        'round_prefer_ceil':
            lambda x: cast(cuda_floor(x + 0.5), 'int32'),
        'floor':
            lambda x: cast(cuda_floor(x + 1e-5), 'int32'),  # add epsilon (1e-5) to prevent gpu rounding error
        'ceil':
            lambda x: cast(cuda_ceil(x - 1e-5), 'int32')    # sub epsilon (1e-5) to prevent gpu rounding error
    }
    if rounding_method not in func_map:
        raise ValueError('Unsupported rounding_method: {}, candidates: {}'.format(rounding_method, func_map.keys()))
    return func_map[rounding_method](x)


def get_2d_pixel(data: TensorInput, n, c, h, w) -> Expr:
    height, width = data.const_shape()[2:]
    h = cuda_max(0, cuda_min(height, h))
    w = cuda_max(0, cuda_min(width, w))
    return data[n, c, h, w]


def linear_interpolate(a, b, ratio):
    return a * (1.0 - ratio) + b * ratio


def resize2d_nchw_task(data: TensorInput, size: List[int], method: str, coordinate_transformation_mode, rounding_method,
                       roi, cubic_alpha, cubic_exclude, extrapolation_value) -> Task:
    image_size = data.const_shape()[2:]
    target_size = size

    def fmap(n, c, h, w):
        h = get_origin_index(h, image_size[0], target_size[0], coordinate_transformation_mode)
        w = get_origin_index(w, image_size[1], target_size[1], coordinate_transformation_mode)
        if method == 'nearest':
            h = get_closest_index(h, rounding_method)
            w = get_closest_index(w, rounding_method)
            value = get_2d_pixel(data, n, c, h, w)
        elif method == 'linear':
            h_int = cast(cuda_floor(h), 'int32')
            w_int = cast(cuda_floor(w), 'int32')
            h_ratio = h - h_int
            w_ratio = w - w_int
            pixels = [[get_2d_pixel(data, n, c, h_int + i, w_int + j) for j in range(2)] for i in range(2)]
            top = linear_interpolate(*pixels[0], w_ratio)
            bottom = linear_interpolate(*pixels[1], w_ratio)
            value = linear_interpolate(top, bottom, h_ratio)
        elif method == 'cubic':
            raise NotImplementedError(method)
        else:
            raise ValueError('Unsupported scaling method: {}, candidates: {}'.format(
                method, ['nearest', 'linear', 'cubic']
            ))
        if coordinate_transformation_mode == 'tf_half_pixel_for_nn':
            value = if_then_else(And.join(0 <= h, h < image_size[0], 0 <= w, w < image_size[1]), value, extrapolation_value)
        return value

    output_shape = data.const_shape()[:2] + list(target_size)
    out = compute(
        'out',
        shape=output_shape,
        fcompute=fmap,
        scope=data.data_type.scope
    )
    return Task(
        'resize2d',
        computation=out,
        params=[data, out],
        worker=Grid()
    )


class Resize2dOp(Operator):
    supported_methods = ['nearest', 'linear', 'cubic']
    supported_coord_trans_mode = ['half_pixel', 'align_corners', 'asymmetric', 'pytorch_half_pixel', 'tf_half_pixel_for_nn', 'tf_crop_and_resize']
    supported_rounding_methods = ['round', 'floor', 'ceil']

    def __init__(self, data, size: List[int], method: str, coordinate_transformation_mode: str, rounding_method: str,
                 roi: Optional, cubic_alpha: Optional, cubic_exclude: Optional, extrapolation_value: Optional):
        if method not in self.supported_methods:
            raise ValueError("Resize only support methods: {}, but got {}.".format(self.supported_methods, method))
        if coordinate_transformation_mode not in self.supported_coord_trans_mode:
            raise ValueError("Resize only support coordinate transformation modes: {}, but got {}.".format(
                self.supported_coord_trans_mode, coordinate_transformation_mode))
        if method == 'nearest' and rounding_method not in self.supported_rounding_methods:
            raise ValueError("Resize only support rounding methods: {}, but got {}.".format(self.supported_rounding_methods, rounding_method))
        if len(size) != 2:
            raise ValueError('Resize2d expect size has 2 elements (height, width), got {}'.format(size))

        super().__init__(
            inputs=[data],
            task=resize2d_nchw_task(input_like(data, 'data'), size, method, coordinate_transformation_mode, rounding_method,
                                    roi, cubic_alpha, cubic_exclude, extrapolation_value),
            method=method,
            coordinate_transformation_mode=coordinate_transformation_mode,
            rounding_method=rounding_method,
            roi=roi,
            cubic_alpha=cubic_alpha,
            cubic_exclude=cubic_exclude,
            extrapolation_value=extrapolation_value
        )


def resize2d(data: Tensor, size: List[int], method: str, coordinate_transformation_mode: str, rounding_method: str, roi: Optional, cubic_alpha: Optional, cubic_exclude: Optional, extrapolation_value: Optional) -> Tensor:
    return Resize2dOp(data, size, method, coordinate_transformation_mode, rounding_method, roi, cubic_alpha, cubic_exclude, extrapolation_value).get_output(0)
