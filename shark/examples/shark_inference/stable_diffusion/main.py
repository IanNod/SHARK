import os

os.environ["AMD_ENABLE_LLPC"] = "1"

from transformers import CLIPTextModel, CLIPTokenizer
import torch
from PIL import Image
import torchvision.transforms as T
from diffusers import (
    LMSDiscreteScheduler,
    PNDMScheduler,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerDiscreteScheduler,
)
from tqdm.auto import tqdm
import numpy as np
from stable_args import args
from utils import get_shark_model, set_iree_runtime_flags
from opt_params import get_unet, get_vae, get_clip
from schedulers import (
    SharkEulerDiscreteScheduler,
)
import time
import sys
from shark.iree_utils.compile_utils import dump_isas
import multiprocessing

clip_q = multiprocessing.Queue()
unet_q = multiprocessing.Queue()
vae_q = multiprocessing.Queue()
time_q = multiprocessing.Queue()

def run_clip(text_input):
    clip = get_clip()
    dtype = torch.float32 if args.precision == "fp32" else torch.half

    if args.warmup_count >= 1:
        clip_warmup_input = torch.randint(1, 2, (2, args.max_length))
    for i in range(args.warmup_count):
        clip.forward((clip_warmup_input,))

    clip_inf_start = time.time()
    text_embeddings = clip.forward((text_input,))
    clip_inf_end = time.time()
    text_embeddings = torch.from_numpy(text_embeddings).to(dtype)
    text_embeddings_numpy = text_embeddings.detach().numpy()

    clip_inf_time = (clip_inf_end - clip_inf_start) * 1000
    print(f"Clip Inference time (ms) = {clip_inf_time:.3f}", flush=True)

    clip_q.put(text_embeddings_numpy)
    time_q.put(clip_inf_end - clip_inf_start)

def run_unet_loop(latents, guidance_scale):
    unet = get_unet()
    dtype = torch.float32 if args.precision == "fp32" else torch.half
    scheduler = DPMSolverMultistepScheduler.from_pretrained(
        "CompVis/stable-diffusion-v1-4",
        subfolder="scheduler",
    )
    cpu_scheduling = True
    if args.version == "v2.1":
        tokenizer = CLIPTokenizer.from_pretrained(
            "stabilityai/stable-diffusion-2-1", subfolder="tokenizer"
        )

        scheduler = DPMSolverMultistepScheduler.from_pretrained(
            "stabilityai/stable-diffusion-2-1",
            subfolder="scheduler",
        )

    if args.version == "v2.1base":
        tokenizer = CLIPTokenizer.from_pretrained(
            "stabilityai/stable-diffusion-2-1-base", subfolder="tokenizer"
        )

        if args.use_compiled_scheduler:
            scheduler = SharkEulerDiscreteScheduler.from_pretrained(
                "stabilityai/stable-diffusion-2-1-base",
                subfolder="scheduler",
            )
            scheduler.compile()
            cpu_scheduling = False
        else:
            scheduler = EulerDiscreteScheduler.from_pretrained(
                "stabilityai/stable-diffusion-2-1-base",
                subfolder="scheduler",
            )
    
    text_embeddings_numpy = clip_q.get()

    total_unet_time_start = time.time()
    scheduler.set_timesteps(num_inference_steps)
    scheduler.is_scale_input_called = True

    latents = latents * scheduler.init_noise_sigma

    avg_ms = 0
    for i, t in tqdm(enumerate(scheduler.timesteps), disable=args.hide_steps):
        step_start = time.time()
        if not args.hide_steps:
            print(f"i = {i} t = {t}", end="", flush=True)
        timestep = torch.tensor([t]).to(dtype).detach().numpy()
        latent_model_input = scheduler.scale_model_input(latents, t)
        if cpu_scheduling:
            latent_model_input = scheduler.scale_model_input(latents, t)

        profile_device = start_profiling(file_path="unet.rdc")

        noise_pred = unet.forward(
            (
                latent_model_input,
                timestep,
                text_embeddings_numpy,
                guidance_scale,
            ),
            send_to_host=False,
        )

        end_profiling(profile_device)
        if cpu_scheduling:
            noise_pred = torch.from_numpy(noise_pred.to_host())
            latents = scheduler.step(noise_pred, t, latents).prev_sample
        else:
            latents = scheduler.step(noise_pred, t, latents)
        step_time = time.time() - step_start
        avg_ms += step_time
        step_ms = int((step_time) * 1000)
        if not args.hide_steps:
            print(f" ({step_ms}ms)", flush=True)

    latents_numpy = latents
    if cpu_scheduling:
        latents_numpy = latents.detach().numpy()

    avg_ms = 1000 * avg_ms / args.steps
    print(f"Average step time: {avg_ms}ms/it", flush=True)
    time_q.put(time.time() - total_unet_time_start) 
    unet_q.put(latents_numpy)

def run_vae():
    vae = get_vae()
    if args.warmup_count >= 1:
        dtype = torch.float32 if args.precision == "fp32" else torch.half
        warmup_latents = torch.randn(
            (batch_size, 4, height // 8, width // 8),
            generator=generator,
            dtype=torch.float32,
        ).to(dtype)
        vae_warmup_input = torch.clone(latents).detach().numpy()
    for i in range(args.warmup_count):
        vae.forward((vae_warmup_input,))

    latents_numpy = unet_q.get()
    total_unet_time_start = time.time()
    profile_device = start_profiling(file_path="vae.rdc")
    vae_start = time.time()
    images = vae.forward((latents_numpy,))
    vae_end = time.time()
    end_profiling(profile_device)
    vae_inf_time = (vae_end - vae_start) * 1000
    print(f"VAE Inference time (ms): {vae_inf_time:.3f}", flush=True)
    vae_q.put(images)
    time_q.put(time.time() - total_unet_time_start)


# Helper function to profile the vulkan device.
def start_profiling(file_path="foo.rdc", profiling_mode="queue"):
    if args.vulkan_debug_utils and "vulkan" in args.device:
        import iree

        print(f"Profiling and saving to {file_path}.")
        vulkan_device = iree.runtime.get_device(args.device)
        vulkan_device.begin_profiling(mode=profiling_mode, file_path=file_path)
        return vulkan_device
    return None


def end_profiling(device):
    if device:
        return device.end_profiling()


if __name__ == "__main__":

    dtype = torch.float32 if args.precision == "fp32" else torch.half

    prompt = args.prompts
    neg_prompt = args.negative_prompts
    height = 512  # default height of Stable Diffusion
    width = 512  # default width of Stable Diffusion
    if args.version == "v2.1":
        height = 768
        width = 768

    num_inference_steps = args.steps  # Number of denoising steps

    # Scale for classifier-free guidance
    guidance_scale = torch.tensor(args.guidance_scale).to(torch.float32)

    generator = torch.manual_seed(
        args.seed
    )  # Seed generator to create the inital latent noise

    # TODO: Add support for batch_size > 1.
    batch_size = len(prompt)
    if batch_size != 1:
        sys.exit("More than one prompt is not supported yet.")
    if batch_size != len(neg_prompt):
        sys.exit("prompts and negative prompts must be of same length")

    set_iree_runtime_flags()
    if args.dump_isa:
        dump_isas(args.dispatch_benchmarks_dir)

    tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")

    # create a random initial latent.
    latents = torch.randn(
        (batch_size, 4, height // 8, width // 8),
        generator=generator,
        dtype=torch.float32,
    ).to(dtype)

    text_input = tokenizer(
        prompt,
        padding="max_length",
        max_length=args.max_length,
        truncation=True,
        return_tensors="pt",
    )
    max_length = text_input.input_ids.shape[-1]
    uncond_input = tokenizer(
        neg_prompt,
        padding="max_length",
        max_length=max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_input = torch.cat([uncond_input.input_ids, text_input.input_ids])

    total_time = 0
    clip_process = multiprocessing.Process(target=run_clip, args=(text_input,))
    clip_process.start()

    unet_process = multiprocessing.Process(target=run_unet_loop, args=(latents, guidance_scale))
    total_time += time_q.get()
    unet_process.start()
    print(total_time)
    vae_process = multiprocessing.Process(target=run_vae)
    total_time += time_q.get()
    vae_process.start()
    print(total_time)
    clip_process.join()
    unet_process.join()
    images = vae_q.get()
    total_time += time_q.get()
    vae_process.join()
    print(total_time)
    print(f"Total image generation runtime (s): {total_time:.4f}")

    transform = T.ToPILImage()
    pil_images = [
        transform(image) for image in torch.from_numpy(images).to(torch.uint8)
    ]
    for i in range(batch_size):
        pil_images[i].save(f"{args.prompts[i]}_{i}.jpg")
