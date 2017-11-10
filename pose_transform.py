from keras.models import Input, Model
from keras.engine.topology import Layer
from keras.backend import tf as ktf


import pose_utils
import pylab as plt
import numpy as np
from skimage.io import imread
from skimage.transform import warp_coords

import skimage.measure
import skimage.transform


from pose_utils import LABELS, MISSING_VALUE
from tensorflow.contrib.image import transform as tf_affine_transform


class AffineTransformLayer(Layer):
    def __init__(self, number_of_transforms, aggregation_fn, init_image_size, **kwargs):
        assert aggregation_fn in ['none', 'max', 'avg']
        self.aggregation_fn = aggregation_fn
        self.number_of_transforms = number_of_transforms
        self.init_image_size = init_image_size
        super(AffineTransformLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        self.image_size = list(input_shape[0][1:])
        print (self.init_image_size)
        self.affine_mul = [1, 1, self.init_image_size[0] / self.image_size[0],
                           1, 1, self.init_image_size[1] / self.image_size[1],
                           1, 1]
        self.affine_mul = np.array(self.affine_mul).reshape((1, 1, 8))

    def call(self, inputs):
        expanded_tensor = ktf.expand_dims(inputs[0], -1)
        multiples = [1, self.number_of_transforms, 1, 1, 1]
        tiled_tensor = ktf.tile(expanded_tensor, multiples=multiples)
        repeated_tensor = ktf.reshape(tiled_tensor, ktf.shape(inputs[0]) * np.array([self.number_of_transforms, 1, 1, 1]))

        affine_transforms = inputs[1] / self.affine_mul

        affine_transforms = ktf.reshape(affine_transforms, (-1, 8))
        tranformed = tf_affine_transform(repeated_tensor, affine_transforms)
        res = ktf.reshape(tranformed, [-1, self.number_of_transforms] + self.image_size)
        res = ktf.transpose(res, [0, 2, 3, 1, 4])

        #Use masks
        if len(inputs) == 3:
            mask = ktf.transpose(inputs[2], [0, 2, 3, 1])
            mask = ktf.image.resize_images(mask, self.image_size[:2], method=ktf.image.ResizeMethod.NEAREST_NEIGHBOR)
            res = res * ktf.expand_dims(mask, axis=-1)


        if self.aggregation_fn == 'none':
            res = ktf.reshape(res, [-1] + self.image_size[:2] + [self.image_size[2] * self.number_of_transforms])
        elif self.aggregation_fn == 'max':
            res = ktf.reduce_max(res, reduction_indices=[-2])
        elif self.aggregation_fn == 'avg':
            counts = ktf.reduce_sum(mask, reduction_indices=[-1])
            counts = ktf.expand_dims(counts, axis=-1)
            res = ktf.reduce_sum(res, reduction_indices=[-2])
            res /= counts
            res = ktf.where(ktf.is_nan(res), ktf.zeros_like(res), res)
        return res

    def compute_output_shape(self, input_shape):
        if self.aggregation_fn == 'none':
            return tuple([input_shape[0][0]] + self.image_size[:2] + [self.image_size[2] * self.number_of_transforms])
        else:
            return input_shape[0]

    def get_config(self):
        config = {"number_of_transforms": self.number_of_transforms,
                  "aggregation_fn": self.aggregation_fn}
        base_config = super(AffineTransformLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


def give_name_to_keypoints(array):
    res = {}
    for i, name in enumerate(LABELS):
        if array[i][0] != MISSING_VALUE and array[i][1] != MISSING_VALUE:
            res[name] = array[i][::-1]
    return res


def check_valid(kp_array):
    kp = give_name_to_keypoints(kp_array)
    return check_keypoints_present(kp, ['Rhip', 'Lhip', 'Lsho', 'Rsho'])


def check_keypoints_present(kp, kp_names):
    result = True
    for name in kp_names:
        result = result and (name in kp)
    return result


def compute_st_distance(kp):
    st_distance1 = np.sum((kp['Rhip'] - kp['Rsho']) ** 2)
    st_distance2 = np.sum((kp['Lhip'] - kp['Lsho']) ** 2)
    return np.sqrt((st_distance1 + st_distance2)/2.0)


def mask_from_kp_array(kp_array, border_inc, img_size):
    min = np.min(kp_array, axis=0)
    max = np.max(kp_array, axis=0)
    min -= int(border_inc)
    max += int(border_inc)

    min = np.maximum(min, 0)
    max = np.minimum(max, img_size[::-1])

    mask = np.zeros(img_size)
    mask[min[1]:max[1], min[0]:max[0]] = 1
    return mask


def get_array_of_points(kp, names):
    return np.array([kp[name] for name in names])


def pose_masks(array2, img_size):
    kp2 = give_name_to_keypoints(array2)
    masks = []
    st2 = compute_st_distance(kp2)
    empty_mask = np.zeros(img_size)

    body_mask = mask_from_kp_array(get_array_of_points(kp2, ['Rhip', 'Lhip', 'Lsho', 'Rsho']), 0.1 * st2, img_size)
    masks.append(body_mask)

    #head present
    if 'nose' in kp2:
        head_mask = mask_from_kp_array(get_array_of_points(kp2, ['nose']), 0.30 * st2, img_size)
        masks.append(head_mask)
    else:
        masks.append(empty_mask)

    def mask_joint(fr, to):
        if not check_keypoints_present(kp2, [fr, to]):
            return empty_mask
        return mask_from_kp_array(get_array_of_points(kp2, [fr, to]), 0.2 * st2, img_size)

    masks.append(mask_joint('Rkne', 'Rhip'))
    masks.append(mask_joint('Lkne', 'Lhip'))

    masks.append(mask_joint('Rank', 'Rkne'))
    masks.append(mask_joint('Lank', 'Lkne'))

    masks.append(mask_joint('Relb', 'Rsho'))
    masks.append(mask_joint('Lelb', 'Lsho'))

    masks.append(mask_joint('Rwri', 'Relb'))
    masks.append(mask_joint('Lwri', 'Lelb'))

    return np.array(masks)


def affine_transforms(array1, array2, img):
    kp1 = give_name_to_keypoints(array1)
    kp2 = give_name_to_keypoints(array2)

    no_point_tr = np.array([[1, 0, 1000], [0, 1, 1000], [0, 0, 1]])

    transforms = []
    def to_transforms(tr):
        if np.linalg.cond(tr) < 10000:
            transforms.append(tr)
        else:
            transforms.append(no_point_tr)

    body_poly_1 = get_array_of_points(kp1, ['Rhip', 'Lhip', 'Lsho', 'Rsho'])
    body_poly_2 = get_array_of_points(kp2, ['Rhip', 'Lhip', 'Lsho', 'Rsho'])
    tr = skimage.transform.estimate_transform('affine', src=body_poly_2, dst=body_poly_1)
    from skimage.transform import warp
    from skimage import img_as_uint
    plt.imshow(warp(img, tr.inverse).astype(np.uint8))

    to_transforms(tr.params)

    head_candidate_names = {'Leye', 'Reye', 'Lear', 'Rear', 'nose'}
    head_kp_names = set()
    for cn in head_candidate_names:
        if cn in kp1 and cn in kp2:
            head_kp_names.add(cn)
    if len(head_kp_names) != 0:
        if len(head_kp_names) < 3:
            head_kp_names.add('Lsho')
            head_kp_names.add('Rsho')
        head_poly_1 = get_array_of_points(kp1, list(head_kp_names))
        head_poly_2 = get_array_of_points(kp2, list(head_kp_names))
        tr = skimage.transform.estimate_transform('affine', dst=head_poly_2, src=head_poly_1)
        to_transforms(tr.params)
    else:
        to_transforms(no_point_tr)

    def estimate_join(fr, to, anchor):
        if not check_keypoints_present(kp2, [fr, to]):
            return no_point_tr
        poly_2 = get_array_of_points(kp2, [fr, to, anchor])
        if check_keypoints_present(kp1, [fr, to]):
            poly_1 = get_array_of_points(kp1, [fr, to, anchor])
        else:
            if fr[0]=='R':
                fr = fr.replace('R', 'L')
                to = to.replace('R', 'L')
                anchor = anchor.replace('R', 'L')
            else:
                fr = fr.replace('L', 'R')
                to = to.replace('L', 'R')
                anchor = anchor.replace('L', 'R')
            if check_keypoints_present(kp1, [fr, to]):
                poly_1 = get_array_of_points(kp1, [fr, to, anchor])
            else:
                return no_point_tr
        return skimage.transform.estimate_transform('affine', dst=poly_1, src=poly_2).params

    to_transforms(estimate_join('Rhip', 'Rkne', 'Rsho'))
    to_transforms(estimate_join('Lhip', 'Lkne', 'Lsho'))

    to_transforms(estimate_join('Rkne', 'Rank', 'Rsho'))
    to_transforms(estimate_join('Lkne', 'Lank', 'Lsho'))

    to_transforms(estimate_join('Rsho', 'Relb', 'Rhip'))
    to_transforms(estimate_join('Lsho', 'Lelb', 'Lhip'))

    to_transforms(estimate_join('Relb', 'Rwri', 'Rsho'))
    to_transforms(estimate_join('Lelb', 'Lwri', 'Lsho'))

    return np.array(transforms).reshape((-1, 9))[..., :-1]


def estimate_uniform_transform(array1, array2):
    kp1 = give_name_to_keypoints(array1)
    kp2 = give_name_to_keypoints(array2)

    no_point_tr = np.array([[1, 0, 1000], [0, 1, 1000], [0, 0, 1]])

    def check_invertible(tr):
        return np.linalg.cond(tr) < 10000

    keypoint_names = {'Rhip', 'Lhip', 'Lsho', 'Rsho'}
    candidate_names = {'Rkne', 'Lkne'}

    for cn in candidate_names:
        if cn in kp1 and cn in kp2:
            keypoint_names.add(cn)

    poly_1 = get_array_of_points(kp1, list(keypoint_names))
    poly_2 = get_array_of_points(kp2, list(keypoint_names))

    tr = skimage.transform.estimate_transform('affine', src=poly_2, dst=poly_1)

    tr = tr.params

    if check_invertible(tr):
        return tr.reshape((-1, 9))[..., :-1]
    else:
        return no_point_tr.reshape((-1, 9))[..., :-1]


if __name__ == "__main__":
    import pandas as pd
    import os
    from skimage.transform import resize
    pairs_df = pd.read_csv('data/fasion-pairs-train.csv')
    kp_df = pd.read_csv('data/fasion-annotation-train.csv', sep=':')
    img_folder = 'data/fasion-dataset/train'
    for _, row in pairs_df.iterrows():
        fr = row['from']
        to = row['to']
        fr_img = imread(os.path.join(img_folder, fr))
        to_img = imread(os.path.join(img_folder, to))

        kp_fr = kp_df[kp_df['name'] == fr].iloc[0]
        kp_to = kp_df[kp_df['name'] == to].iloc[0]

        plt.subplot(3, 1, 1)
        kp_fr = pose_utils.load_pose_cords_from_strings(kp_fr['keypoints_y'], kp_fr['keypoints_x'])
        kp_to = pose_utils.load_pose_cords_from_strings(kp_to['keypoints_y'], kp_to['keypoints_x'])

        img = fr_img.copy()
        p, m = pose_utils.draw_pose_from_cords(kp_fr, img.shape[:2])
        img[m] = p[m]
        plt.imshow(img)

        plt.subplot(3, 1, 2)
        img = to_img.copy()
        p, m = pose_utils.draw_pose_from_cords(kp_to, img.shape[:2])
        img[m] = p[m]
        plt.imshow(img)

        p = resize(p, (256, 256), preserve_range=True).astype(np.uint8)
        m = resize(m, (256, 256), preserve_range=True).astype(bool)
        fr_img = resize(fr_img, (256, 256), preserve_range=True).astype(float)

        tr = affine_transforms(kp_fr, kp_to, fr_img)
        masks = pose_masks(kp_to, fr_img.shape[:2])

        print ([np.sum(mask) for mask in masks])

        x = Input(fr_img.shape)
        i = Input((10, 8))
        mm = Input([10] + list(fr_img.shape[:2]))

        y = AffineTransformLayer(10, 'none', (256, 256))([x, i, mm])
        model = Model(inputs=[x, i, mm], outputs=y)

        b = model.predict([fr_img[np.newaxis], tr[np.newaxis], masks[np.newaxis]])
        plt.subplot(3, 1, 3)

        a = b[0, ..., 0:3].copy().astype('uint8')
        a[m] = p[m]
        plt.imshow(a)

        plt.show()



    # def get(first, second):
    #     plt.subplot(3, 1, 1)
    #     a = first
    #     img = imread(a[0])
    #     image = img.copy()
    #     array1 = pose_utils.load_pose_cords_from_strings(a[2], a[1])
    #     print (img.shape)
    #     p, m = pose_utils.draw_pose_from_cords(array1, img.shape[:2])
    #     img[m] = p[m]
    #     plt.imshow(img)
    #
    #     plt.subplot(3, 1, 2)
    #     a = second
    #     img = imread(a[0])
    #     array2 = pose_utils.load_pose_cords_from_strings(a[2], a[1])
    #     p, m = pose_utils.draw_pose_from_cords(array2, img.shape[:2])
    #     img[m] = p[m]
    #     plt.imshow(img)
    #     pose_utils.draw_legend()
    #     trs = affine_transforms(array1, array2)
    #     masks = pose_masks(array2, (128, 64))
    #
    #     image = resize(image, (64, 32), preserve_range=True)
    #     m = resize(m, (64, 32), preserve_range=True, order=0).astype(bool)
    #     p = resize(p, (64, 32), preserve_range=True, order=0)
    #     return trs, masks, image, p,  m
    #
    #
    # trs, masks, img, p, m = get(first2, second2)
    # trs2, masks2, img2, p2, m2 = get(first, second)
    #
    # plt.subplot(3, 1, 3)
    #
    # x_v = np.concatenate([img[np.newaxis], img2[np.newaxis]])
    # i_v = np.concatenate([trs[np.newaxis], trs2[np.newaxis]])
    # m_v = np.concatenate([masks[np.newaxis], masks2[np.newaxis]])
    #
    # #trs = CordinatesWarp.affine_transforms(array1, array2)
    # x = Input((64,32,3))
    # i = Input((len(trs), 8))
    # masks = Input((len(trs), 128, 64))
    #
    # y = AffineTransformLayer(len(trs), 'max', (128, 64))([x, i])
    # model = Model(inputs=[x, i, masks], outputs=y)
    #
    # # x_v = skimage.transform.resize(image, (128, 64), preserve_range=True)[np.newaxis]
    # # i_v = trs[np.newaxis]
    #
    # b = model.predict([x_v, i_v, m_v])
    # print (b.shape)
    # b = b[..., :3]
    #
    #
    # # trs, _ = CordinatesWarp.warp_mask(array1, array2, img_size=img.shape[:2])
    # # x = Input((128,64,3))
    # # i = Input((128,64,2))
    # #
    # # y = WarpLayer(1)([x, i])
    # # model = Model(inputs=[x, i], outputs=y)
    # #
    # # x_v = skimage.transform.resize(image, (128, 64), preserve_range=True)[np.newaxis]
    # # i_v = trs[np.newaxis]
    # #
    # # b = model.predict([x_v, i_v])
    # # print (b.shape)
    #
    # warped_image = np.squeeze(b[1]).astype(np.uint8)
    # warped_image[m2] = p2[m2]
    # plt.imshow(warped_image)
    #
    # # from scipy.ndimage import map_coordinates
    # #
    # # mask = CordinatesWarp.warp_mask(array1, array2, (128, 64, 3))
    # # mask = np.moveaxis(mask, -1, 0)
    # # warped_image = map_coordinates(image, mask)
    # # warped_image[m] = p[m]
    # # plt.subplot(4, 1, 4)
    # # plt.imshow(warped_image)
    # plt.show()
