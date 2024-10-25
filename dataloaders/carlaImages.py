import os
import json
import math
import numpy as np
from PIL import Image
from torch.utils import data
from dataloaders.transforms import train_transform, val_transform, canbus_normalization

from configs import g_conf


class carlaImages(data.Dataset):

    def __init__(self, model_name, base_dir, dataset_list, split="train"):
        print('')
        self.root = base_dir
        self.split = split
        self.data = []
        self.data_in_chunk = []
        self.model_name = model_name
        self.dataset_name = ''

        for dataset_name in dataset_list:
            self.dataset_name += dataset_name.split('/')[-1]

        for dataset_name in dataset_list:
            self.images_base = os.path.join(self.root, dataset_name)
            # 针对不同的模型，我们设置了不同的数据加载策略，并且保存了npy文件，以便下次更好的加载
            canbus_paths = self.recursive_glob(rootdir=self.images_base, prefix='cmd_fix', suffix='.json')
            all_cam_paths_dict = {}
            for camera_type in g_conf.DATA_USED:
                img_paths = self.recursive_glob(rootdir=self.images_base, prefix=camera_type, suffix='.png')
                all_cam_paths_dict.update({camera_type: img_paths})
            self.data = self._add_canbus_data_point(self.data, all_cam_paths_dict, canbus_paths)

            # 对于多帧输入，我们还需要确保这些帧来自同一剧集episode
            self.data_in_chunk = self.get_episode_chunk(self.data_in_chunk, rootdir=self.images_base,
                                                        prefix='cmd_fix', suffix='.json')

        index_list = list(range(0, len(self.data)))
        index_chunks = []
        count = 0
        for chunk in self.data_in_chunk:
            index_chunks.append(index_list[count:count + len(chunk)])
            count += len(chunk)

        self.block_index_start = []
        self.block_index_end = []
        block_num_start = (g_conf.ENCODER_INPUT_FRAMES_NUM - 1) * g_conf.ENCODER_STEP_INTERVAL
        block_num_end = (g_conf.DECODER_OUTPUT_FRAMES_NUM - g_conf.ENCODER_INPUT_FRAMES_NUM + g_conf.ENCODER_OUTPUT_STEP_DELAY) * g_conf.ENCODER_STEP_INTERVAL

        print(split)
        print('  - number of chunks:', len(index_chunks))
        print('  - block number of data per chunk:', 'begining:', block_num_start, 'end:', block_num_end)
        if block_num_start > 0:
            for chunk in index_chunks:
                self.block_index_start += chunk[:block_num_start]
        if block_num_end > 0:
            for chunk in index_chunks:
                self.block_index_end += chunk[-block_num_end:]
        print('  - total data blocked:', len(self.block_index_start + self.block_index_end))

    def __len__(self):
        return len(self.data)

    def analyze_index(self, index):
        if index in self.block_index_start:
            index += (g_conf.ENCODER_INPUT_FRAMES_NUM - 1) * g_conf.ENCODER_STEP_INTERVAL

        elif index in self.block_index_end:
            index -= (g_conf.DECODER_OUTPUT_FRAMES_NUM - g_conf.ENCODER_INPUT_FRAMES_NUM + g_conf.ENCODER_OUTPUT_STEP_DELAY) * g_conf.ENCODER_STEP_INTERVAL

        return index

    def __getitem__(self, index):
        # 我们尽量避免“列表超出范围问题”。由于我们可能使用多个框架作为输入或输出，因此索引不应位于最后几个点。
        # 此外，我们还让这些帧来自同一集，这意味着它们需要是连续的
        index = self.analyze_index(index)

        data_vec = {'current': [], 'future': []}
        for n in range(g_conf.ENCODER_INPUT_FRAMES_NUM):
            datapoint = self.data[index - (g_conf.ENCODER_INPUT_FRAMES_NUM - 1 - n) * g_conf.ENCODER_STEP_INTERVAL]
            sample = {'can_bus': datapoint['can_bus']}
            for camera_type in g_conf.DATA_USED:
                img = Image.open(datapoint[camera_type]).convert('RGB')
                sample.update({camera_type: img})

            if self.split == 'train':
                one_frame_data = self.transform_tr(sample)
                data_vec['current'].append(one_frame_data)

            elif self.split == 'val':
                one_frame_data = self.transform_val(sample)
                data_vec['current'].append(one_frame_data)

        # 输出有时间延迟
        if g_conf.ENCODER_OUTPUT_STEP_DELAY > 0 or g_conf.DECODER_OUTPUT_FRAMES_NUM != g_conf.ENCODER_INPUT_FRAMES_NUM:
            for o in range(g_conf.DECODER_OUTPUT_FRAMES_NUM):
                datapoint_future = self.data[index - (
                            g_conf.ENCODER_INPUT_FRAMES_NUM - 1 - o - g_conf.ENCODER_OUTPUT_STEP_DELAY) * g_conf.ENCODER_STEP_INTERVAL]
                sample_future = {'can_bus_future': datapoint_future['can_bus']}
                data_vec['future'].append(sample_future)
                ### We comment the future image to speed up training
                # img_future = Image.open(datapoint_future['image']).convert('RGB')
                # sample_future = {'image_future': img_future, 'can_bus_future': datapoint_future['can_bus']}
                # if self.split == 'train':
                #     one_frame_data_future = self.transform_tr(sample_future)
                #     data_vec['future'].append(one_frame_data_future)

                # elif self.split == 'val':
                #     one_frame_data_future = self.transform_val(sample_future)
                #     data_vec['future'].append(one_frame_data_future)

                # del one_frame_data_future
                # del img_future
            del sample_future
            del datapoint_future

        del one_frame_data
        del sample
        del datapoint
        del img

        return data_vec

    def _add_canbus_data_point(self, full_dataset, img_paths_dict, canbus_paths):
        """
            向完整数据集的向量中添加一个数据点
            :param full_dataset:
            :param img_paths:
            :param canbus_paths:
            :param camera_type: 应用于转向的增强相机类型。
            :return:
            """
        for camera_type, img_paths in img_paths_dict.items():
            if len(img_paths) != len(canbus_paths):
                print(camera_type, len(img_paths), len(canbus_paths))
                raise RuntimeError('The nubmers of images and canbus data are not mathced!!')

        for i in range(len(canbus_paths)):
            datapoint = {}
            datapoint['can_bus'] = dict()
            f = open(canbus_paths[i], 'r')
            canbus_data = json.loads(f.read())
            for value in g_conf.TARGETS + g_conf.OTHER_INPUTS:
                datapoint['can_bus'][value] = canbus_data[value]
            datapoint['can_bus'] = canbus_normalization(datapoint['can_bus'], g_conf.DATA_NORMALIZATION)
            for camera_type, img_paths in img_paths_dict.items():
                datapoint[camera_type] = img_paths[i]
            full_dataset.append(datapoint)

        return full_dataset

    def recursive_glob(self, rootdir='.', prefix='', suffix=''):
        """使用给定的后缀和 rootdir 执行递归 glob
            :param rootdir 是根目录
            :param prefix 是要搜索的起始前缀
            :param suffix 是要搜索的后缀
        """
        return [os.path.join(looproot, filename)
                for looproot, _, filenames in sorted(os.walk(rootdir))
                for filename in sorted(filenames) if filename.startswith(prefix) and filename.endswith(suffix)]

    def get_episode_chunk(self, data, rootdir='.', prefix='', suffix=''):
        for looproot, _, filenames in sorted(os.walk(rootdir)):
            files = []
            for filename in sorted(filenames):
                if filename.startswith(prefix) and filename.endswith(suffix):
                    files.append(os.path.join(looproot, filename))
            if files:
                data.append(files)
        return data

    def transform_tr(self, sample):
        return train_transform(sample, g_conf.IMAGE_SHAPE, augmentation=g_conf.AUGMENTATION)

    def transform_val(self, sample):
        return val_transform(sample, g_conf.IMAGE_SHAPE)

