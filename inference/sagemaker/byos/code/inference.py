# -*- coding: utf-8 -*-
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import os
import json
import uuid
import io
import sys
import tarfile
import traceback
import uuid

from PIL import Image

import requests
import boto3
import sagemaker
import torch
import s3fs

from PIL import Image

from collections import defaultdict
from torch import autocast
from diffusers import StableDiffusionPipeline,StableDiffusionImg2ImgPipeline
from diffusers import AltDiffusionPipeline, AltDiffusionImg2ImgPipeline
from diffusers import EulerDiscreteScheduler, EulerAncestralDiscreteScheduler, HeunDiscreteScheduler, LMSDiscreteScheduler, KDPM2DiscreteScheduler, KDPM2AncestralDiscreteScheduler,DDIMScheduler

from safetensors.torch import load_file, save_file


s3_client = boto3.client('s3')


max_height = os.environ.get("max_height", 768)
max_width = os.environ.get("max_width", 768)
max_steps = os.environ.get("max_steps", 100)
max_count = os.environ.get("max_count", 4)
s3_bucket = os.environ.get("s3_bucket", "")
watermarket=os.environ.get("watermarket", True)
watermarket_image=os.environ.get("watermarket_image", "sagemaker-logo-small.png")
custom_region = os.environ.get("custom_region", None)
safety_checker_enable = json.loads(os.environ.get("safety_checker_enable", "false"))

#add lora support
lora_model = os.environ.get("lora_model", None)

# need add more sampler
samplers = {
    "euler_a": EulerAncestralDiscreteScheduler,
    "eular": EulerDiscreteScheduler,
    "heun": HeunDiscreteScheduler,
    "lms": LMSDiscreteScheduler,
    "dpm2": KDPM2DiscreteScheduler,
    "dpm2_a": KDPM2AncestralDiscreteScheduler,
    "ddim": DDIMScheduler
}




  

def get_bucket_and_key(s3uri):
    """
    get_bucket_and_key is helper function
    """
    pos = s3uri.find('/', 5)
    bucket = s3uri[5: pos]
    key = s3uri[pos + 1:]
    return bucket, key

def untar(fname, dirs):
    """
    :param fname: tar file name
    :param dirs: untar path
    :return: bool
    """
    try:
        t = tarfile.open(fname)
        t.extractall(path = dirs)
        return True
    except Exception as e:
        print(e)
        return False

def load_lora_weights(pipeline, checkpoint_path, multiplier, device, dtype):
    LORA_PREFIX_UNET = "lora_unet"
    LORA_PREFIX_TEXT_ENCODER = "lora_te"
    # load LoRA weight from .safetensors
    state_dict = load_file(checkpoint_path)

    updates = defaultdict(dict)
    for key, value in state_dict.items():
        # it is suggested to print out the key, it usually will be something like below
        # "lora_te_text_model_encoder_layers_0_self_attn_k_proj.lora_down.weight"

        layer, elem = key.split('.', 1)
        updates[layer][elem] = value

    # directly update weight in diffusers model
    for layer, elems in updates.items():

        if "text" in layer:
            layer_infos = layer.split(LORA_PREFIX_TEXT_ENCODER + "_")[-1].split("_")
            curr_layer = pipeline.text_encoder
        else:
            layer_infos = layer.split(LORA_PREFIX_UNET + "_")[-1].split("_")
            curr_layer = pipeline.unet

        # find the target layer
        temp_name = layer_infos.pop(0)
        while len(layer_infos) > -1:
            try:
                curr_layer = curr_layer.__getattr__(temp_name)
                if len(layer_infos) > 0:
                    temp_name = layer_infos.pop(0)
                elif len(layer_infos) == 0:
                    break
            except Exception:
                if len(temp_name) > 0:
                    temp_name += "_" + layer_infos.pop(0)
                else:
                    temp_name = layer_infos.pop(0)

        # get elements for this layer
        weight_up = elems['lora_up.weight'].to(dtype)
        weight_down = elems['lora_down.weight'].to(dtype)
        alpha = elems['alpha']
        if alpha:
            alpha = alpha.item() / weight_up.shape[1]
        else:
            alpha = 1.0

        # update weight
        if len(weight_up.shape) == 4:
            curr_layer.weight.data += multiplier * alpha * torch.mm(weight_up.squeeze(3).squeeze(2), weight_down.squeeze(3).squeeze(2)).unsqueeze(2).unsqueeze(3)
        else:
            curr_layer.weight.data += multiplier * alpha * torch.mm(weight_up, weight_down)

    return pipeline


def init_pipeline(model_name: str,model_args=None,lora_model=None):
    """
    help load model from s3
    """
    print(f"=================init_pipeline: base_model:{model_name}, lora_model:{lora_model}=================")
    
    model_path=model_name
    base_name=os.path.basename(model_name)
    try:    
        if model_name.startswith("s3://"):
            fs = s3fs.S3FileSystem()
            if base_name=="model.tar.gz":
                local_path= "/".join(model_name.split("/")[-2:-1])
                model_path=f"/tmp/{local_path}"
                print(f"need copy {model_name} to {model_path}")
                os.makedirs(model_path)
                fs.get(model_name,model_path+"/", recursive=True)
                untar(f"/tmp/{local_path}/model.tar.gz",model_path)
                os.remove(f"/tmp/{local_path}/model.tar.gz")
                print("download and untar  completed")
            else:
                local_path= "/".join(model_name.split("/")[-2:])
                model_path=f"/tmp/{local_path}"
                print(f"need copy {model_name} to {model_path}")
                os.makedirs(model_path)
                fs.get(model_name,model_path, recursive=True)
                print("download completed")

        print(f"pretrained model_path: {model_path}")
        if model_args is not None:
            pipe=StableDiffusionPipeline.from_pretrained(
                 model_path, **model_args)
        else:
            pipe=StableDiffusionPipeline.from_pretrained(model_path)
        
        #lora model support
        if lora_model is not None:
            if lora_model.startswith("s3://") and '.safetensors' in lora_model:
                fs = s3fs.S3FileSystem()
                _dir_path=str(uuid.uuid4())
                local_lora_path= f"/tmp/{_dir_path}"
                print(local_lora_path)
                os.makedirs(local_lora_path)
                fs.get(lora_model,local_lora_path+"/")
                print(f"download and {lora_model} to {local_lora_path}  completed")
                local_lora_file=local_lora_path+"/"+os.path.basename(lora_model)
                state_dict = load_file(local_lora_file)
                pipe = load_lora_weights(pipe, local_lora_file, 0.5, 'cuda:0', torch.float16)
            
            #TODO support https://   
            #elif lora_model.startswith("https://") and '.safetensors' in lora_model:
                    
            else:
                #use huggingface lora
                pipe.unet.load_attn_procs(lora_model)
               
                
        return pipe
    except Exception as ex:
        traceback.print_exc(file=sys.stdout)
        print(f"=================Exception================={ex}")
        return None

def model_fn(model_dir):
    """
    Load the model for inference,load model from os.environ['model_name'],diffult use stabilityai/stable-diffusion-2
    
    """
    print("=================model_fn=================")
    print(f"model_dir: {model_dir}")
    model_name = os.environ.get("model_name", "stabilityai/stable-diffusion-2")
    model_args = json.loads(os.environ['model_args']) if (
        'model_args' in os.environ) else None
    lora_model = os.environ.get("lora_model", None)
    
    task = os.environ['task'] if ('task' in os.environ) else "text-to-image"
    print(
        f'model_name: {model_name},  model_args: {model_args}, task: {task} ')

    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True

   
    model = init_pipeline(model_name,model_args=model_args,lora_model=lora_model)
    
    if safety_checker_enable is False :
        #model.safety_checker = lambda images, clip_input: (images, False)
        model.safety_checker=None
    model = model.to("cuda")
    model.enable_attention_slicing()

    return model


def input_fn(request_body, request_content_type):
    """
    Deserialize and prepare the prediction input
    """
    # {
    # "prompt": "a photo of an astronaut riding a horse on mars",
    # "negative_prompt":"",
    # "steps":0,
    # "sampler":"",
    # "seed":-1,
    # "height": 512,
    # "width": 512
    # }
    print(f"=================input_fn=================\n{request_content_type}\n{request_body}")
    input_data = json.loads(request_body)
    return prepare_opt(input_data)


def clamp_input(input_data, minn, maxn):
    """
    clamp_input check input 
    """
    return max(min(maxn, input_data), minn)


def prepare_opt(input_data):
    """
    Prepare inference input parameter
    """
    opt = {}
    opt["prompt"] = input_data.get(
        "prompt", "a photo of an astronaut riding a horse on mars")
    opt["negative_prompt"] = input_data.get("negative_prompt", "")
    opt["steps"] = clamp_input(input_data.get(
        "steps", 20), minn=20, maxn=max_steps)
    opt["sampler"] = input_data.get("sampler", None)
    opt["height"] = clamp_input(input_data.get(
        "height", 512), minn=64, maxn=max_height)
    opt["width"] = clamp_input(input_data.get(
        "width", 512), minn=64, maxn=max_width)
    opt["count"] = clamp_input(input_data.get(
        "count", 1), minn=1, maxn=max_count)
    opt["seed"] = input_data.get("seed", 1024)
    opt["input_image"] = input_data.get("input_image", None)

    if opt["sampler"] is not None:
        opt["sampler"] = samplers[opt["sampler"]
                                  ] if opt["sampler"] in samplers else samplers["euler_a"]

    print(f"=================prepare_opt=================\n{opt}")
    return opt


def predict_fn(input_data, model):
    """
    Apply model to the incoming request
    """
    print("=================predict_fn=================")
    print('input_data: ', input_data)
    prediction = []

    try:

        sagemaker_session = sagemaker.Session() if custom_region is None else sagemaker.Session(
            boto3.Session(region_name=custom_region))
        bucket = sagemaker_session.default_bucket()
        if s3_bucket != "":
            bucket = s3_bucket
        default_output_s3uri = f's3://{bucket}/stablediffusion/asyncinvoke/images/'
        output_s3uri = input_data['output_s3uri'] if 'output_s3uri' in input_data else default_output_s3uri
        infer_args = input_data['infer_args'] if (
            'infer_args' in input_data) else None
        print('infer_args: ', infer_args)
        init_image = infer_args['init_image'] if infer_args is not None and 'init_image' in infer_args else None
        input_image = input_data['input_image']
        print('init_image: ', init_image)
        print('input_image: ', input_image)

        # load different Pipeline for txt2img , img2img
        # referen doc: https://huggingface.co/docs/diffusers/api/diffusion_pipeline#diffusers.DiffusionPipeline.components
        #   text2img = StableDiffusionPipeline.from_pretrained("CompVis/stable-diffusion-v1-4")
        #   img2img = StableDiffusionImg2ImgPipeline(**text2img.components)
        #   inpaint = StableDiffusionInpaintPipeline(**text2img.components)
        #  use StableDiffusionImg2ImgPipeline for input_image        
        if input_image is not None:
            response = requests.get(input_image, timeout=5)
            init_img = Image.open(io.BytesIO(response.content)).convert("RGB")
            init_img = init_img.resize(
                (input_data["width"], input_data["height"]))
            model = StableDiffusionImg2ImgPipeline(**model.components)  # need use Img2ImgPipeline
            

        generator = torch.Generator(
            device='cuda').manual_seed(input_data["seed"])

        with autocast("cuda"):
            model.scheduler = input_data["sampler"].from_config(
                model.scheduler.config)
            if input_image is None:
                images = model(input_data["prompt"], input_data["height"], input_data["width"], negative_prompt=input_data["negative_prompt"],
                               num_inference_steps=input_data["steps"], num_images_per_prompt=input_data["count"], generator=generator).images
            else:
                images = model(input_data["prompt"], image=init_img, negative_prompt=input_data["negative_prompt"],
                               num_inference_steps=input_data["steps"], num_images_per_prompt=input_data["count"], generator=generator).images
             # image watermark
            if watermarket:
                crop_image = Image.open(f"/opt/ml/model/{watermarket_image}")
                size = (200, 39)
                crop_image.thumbnail(size)
                if crop_image.mode != "RGBA":
                    crop_image = crop_image.convert("RGBA")
                layer = Image.new("RGBA",[input_data["width"],input_data["height"]],(0,0,0,0))
                layer.paste(crop_image,(input_data["width"]-210, input_data["height"]-49))
            
            for image in images:
                bucket, key = get_bucket_and_key(output_s3uri)
                key = f'{key}{uuid.uuid4()}.jpg'
                buf = io.BytesIO()
                if watermarket:
                    out = Image.composite(layer,image,layer)
                    out.save(buf, format='JPEG')
                else:
                    image.save(buf, format='JPEG')
                
                s3_client.put_object(
                    Body=buf.getvalue(),
                    Bucket=bucket,
                    Key=key,
                    ContentType='image/jpeg',
                    Metadata={
                        # #s3 metadata only support ascii
                        "prompt": input_data["prompt"],
                        "seed": str(input_data["seed"])
                    }
                )
                print('image: ', f's3://{bucket}/{key}')
                prediction.append(f's3://{bucket}/{key}')
    except Exception as ex:
        traceback.print_exc(file=sys.stdout)
        print(f"=================Exception================={ex}")

    print('prediction: ', prediction)
    return prediction


def output_fn(prediction, content_type):
    """
    Serialize and prepare the prediction output
    """
    print(content_type)
    return json.dumps(
        {
            'result': prediction
        }
    )
