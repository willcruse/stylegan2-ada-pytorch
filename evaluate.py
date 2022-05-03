from typing import AnyStr, List, Dict, Tuple, Any
from pathlib import Path
import functools
from multiprocessing import Pool, current_process
from unittest import result

import ruamel.yaml as yaml
import selectivesearch
from skimage import io, color
import numpy as np
import matplotlib.pyplot as plt
import PIL

from predict_beast import BoundingBox, Predictor, RegionProposal, PartSegmentor

def convert_to_rgb(image: np.array) -> np.array:
            
    if len(image.shape) == 2:
        image = color.gray2rgb(image)

    if image.shape[2] == 4: # Image is RGBA
        image = color.rgba2rgb(image)

    return image

def calculate_area(box: BoundingBox) -> float:
    delta_x = abs(box[2] - box[0])
    delta_y = abs(box[3] - box[1])

    return delta_x * delta_y

def calculate_iou(ground_truth: BoundingBox, predicted: BoundingBox) -> float:
    area_gt = calculate_area(ground_truth)
    area_pr = calculate_area(predicted)

    overlap_box = tuple([
        max(ground_truth[0], predicted[0]),
        max(ground_truth[1], predicted[1]),
        min(ground_truth[2], predicted[2]),
        min(ground_truth[3], predicted[3]),
    ])


    area_overlap = calculate_area(overlap_box)


    total_area = area_gt + area_pr - area_overlap
    
    iou = area_overlap / total_area

    if iou < 0 or iou > 1:
        raise Exception(f"Invalid IoU")

    return iou

def region_proposal(image_data: Dict[AnyStr, AnyStr]) -> Tuple[AnyStr, List[Tuple[float, BoundingBox]]]:
    image = io.imread(str(Path("segmented_images") / image_data.get("file-path")))
            
    image = convert_to_rgb(image)

    predicted_boxes = RegionProposal.get_region_proposals(image, scale=900, sigma=1.2, min_size=300)

    ground_truths = image_data.get("bounding-boxes", [])
    iou_values = [(0, None)] * len(ground_truths)

    for box in predicted_boxes:
        for index, ground_truth in enumerate(ground_truths):
            try:
                iou = calculate_iou(ground_truth, box)
            except Exception as e:
                continue
            
            if iou > iou_values[index][0]:
                iou_values[index] = (iou, box)

    return image_data.get("image-id"), iou_values

def evaluate_region_proposal(images: List[Dict[AnyStr, AnyStr]]) -> Dict[AnyStr, List[Tuple[float, BoundingBox]]]:
    iou_results = {}
    with Pool(processes=16) as pool:
        iou_results_generator = pool.imap_unordered(region_proposal, images, chunksize=16)

        for index, iou_result in enumerate(iou_results_generator):
            iou_results[iou_result[0]] = iou_result[1]

            if index % 50 == 0:
                print("Completed: ", index)

    return iou_results

def part_segmentation(image_data: Dict[AnyStr, AnyStr], part_segmentors: Dict[AnyStr, Any]) -> Tuple[AnyStr, List[List[int]]]:
    # Load in image
    image = PIL.Image.open(str(Path("segmented_images") / image_data.get("file-path"))).convert('RGB')

    # Crop beast region
    region_suggestions = image_data.get("bounding-boxes", [])
    
    processed_regions = []
    for region in region_suggestions:
        _, height = image.size

        # Reverse Y - Coord for PIL Crop
        region[1] = height - region[1]
        region[3] = height - region[3]
        cropped = image.crop(region)
        processed_regions.append(cropped)
    
    region_vectors = []
    for processed_region in processed_regions:
        feature_vectors = {}
        for beast, part_segmentor in part_segmentors.items():
            feature_vectors[beast] = part_segmentor.segment_parts(processed_region)

        region_vectors.append(feature_vectors)

    return tuple([image_data["image-id"], region_vectors])

def evaluate_part_segmentation(images: List[Dict[AnyStr, AnyStr]], segmentor_configs: Dict[AnyStr, Dict[AnyStr, Any]]) -> Dict[AnyStr, Dict[AnyStr, int]]:

    part_segmentors = {}

    for beast, segmentor_config in segmentor_configs.items():
        part_segmentors[beast] = PartSegmentor(**segmentor_config)

    segmentation_results = {} # ImageID: [{beast: feature-vector}]

    for index, image_data in enumerate(images, start=1):
        import time
        start = time.perf_counter()
        part_segmentation_result = part_segmentation(image_data, part_segmentors)
        print(f"Took: {(time.perf_counter() - start)} seconds")

        segmentation_results[part_segmentation_result[0]] = part_segmentation_result[1]

        if index % 10 == 0:
            print("Completed: ", index)

    return segmentation_results

if __name__=="__main__":
    metadata_file = Path("./segmented_images/dump.yaml")
    data = yaml.load(metadata_file.read_text(), Loader=yaml.RoundTripLoader)
    config_file_path = Path("./config.yaml")
    
    config_file: Dict[AnyStr, Any] = yaml.load(config_file_path.read_text(), Loader=yaml.RoundTripLoader)

    segmentor_configs = config_file["predictor_config"]["part_segmentor_configs"]

    images = []
    for image_data in data.values():
        if image_data.get("beast") != "pegasus":
            continue

        images.append(image_data)

    import time
    start = time.perf_counter()
    results = evaluate_part_segmentation(images, segmentor_configs)
    print(f"Took: {(time.perf_counter() - start)/60} minutes")
    print(results)
    for image_key, feature_vectors in results.items():
        data[image_key]["feature-vectors"] = feature_vectors

    Path("./evaluate_part_segmentation.yaml").write_text(yaml.dump(data, Dumper=yaml.RoundTripDumper))