import os
from pathlib import Path
from loguru import logger
import torch
from einops import rearrange
import torch.distributed
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader
from hymm_sp.config import parse_args
from hymm_sp.sample_inference import HunyuanVideoSampler
from hymm_sp.data_kits.video_dataset import VideoDataset, AudioDataset, ImageDataset
from hymm_sp.data_kits.data_tools import save_videos_grid
from hymm_sp.modules.parallel_states import (
    initialize_distributed,
    nccl_info,
)

def main():
    args = parse_args()
    models_root_path = Path(args.ckpt)
    print("*"*20) 
    initialize_distributed(args.seed)
    if not models_root_path.exists():
        raise ValueError(f"`models_root` not exists: {models_root_path}")
    print("+"*20)
    # Create save folder to save the samples
    save_path = args.save_path if args.save_path_suffix=="" else f'{args.save_path}_{args.save_path_suffix}'
    if not os.path.exists(args.save_path):
        os.makedirs(save_path, exist_ok=True)

    # Load models
    rank = 0
    vae_dtype = torch.float16
    device = torch.device("cuda")
    if nccl_info.sp_size > 1:
        device = torch.device(f"cuda:{torch.distributed.get_rank()}")
        rank = torch.distributed.get_rank()

    hunyuan_video_sampler = HunyuanVideoSampler.from_pretrained(args.ckpt, args=args, device=device)
    # Get the updated args
    args = hunyuan_video_sampler.args
    
    if args.video_condition:
        dataset = VideoDataset(args, device=device) 
    elif args.audio_condition:
        dataset = AudioDataset(args, device=device) 
    else:
        dataset = ImageDataset(args, device=device)
    sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=False, drop_last=False)
    json_loader = DataLoader(dataset, batch_size=1, shuffle=False, sampler=sampler, drop_last=False)
    for batch_index, batch in enumerate(json_loader, start=1):
        pixel_value_llava = batch['pixel_value_llava'].to(device)
        pixel_value_ref = batch['pixel_value_ref'].to(device)
        uncond_pixel_value_llava = batch['uncond_pixel_value_llava']
        prompt = batch['prompt'][0]
        negative_prompt = batch['negative_prompt'][0]        
        name = batch['name'][0]
        save_name = batch['data_name'][0]
        seed = batch['seed']
        audio_prompts = batch['audio_prompts'][0].to(device) if 'audio_prompts' in batch else None
        audio_path = batch['audio_path'][0] if 'audio_path' in batch else None

        pixel_value_ref = pixel_value_ref * 2 - 1.
        pixel_value_ref_for_vae = rearrange(pixel_value_ref,"b c h w -> b c 1 h w")
        with torch.autocast(device_type="cuda", dtype=vae_dtype, enabled=vae_dtype != torch.float32):
            hunyuan_video_sampler.vae.enable_tiling()
            ref_latents = hunyuan_video_sampler.vae.encode(pixel_value_ref_for_vae.clone()).latent_dist.sample()
            uncond_ref_latents = hunyuan_video_sampler.vae.encode(torch.ones_like(pixel_value_ref_for_vae)).latent_dist.sample()
            if args.video_condition:
                pixel_value_bg = batch['pixel_value_bg'].to(device) * 2 - 1.             
                bg_latents = hunyuan_video_sampler.vae.encode(pixel_value_bg).latent_dist.sample()                
                pixel_value_mask = batch['pixel_value_mask'].to(device) * 2 - 1.
                mask_latents = hunyuan_video_sampler.vae.encode(pixel_value_mask).latent_dist.sample()
                bg_latents = torch.cat([bg_latents, mask_latents], dim=1)
                bg_latents.mul_(hunyuan_video_sampler.vae.config.scaling_factor)
                _, _, t, h, w = bg_latents.shape
                args.video_size = (h * 8, w * 8)
                args.sample_n_frames = (t - 1) * 4 + 1
            else:
                bg_latents = None
            ref_latents.mul_(hunyuan_video_sampler.vae.config.scaling_factor)
            uncond_ref_latents.mul_(hunyuan_video_sampler.vae.config.scaling_factor)
            hunyuan_video_sampler.vae.disable_tiling()
        prompt = args.add_pos_prompt + prompt
        negative_prompt = args.add_neg_prompt + negative_prompt
        outputs = hunyuan_video_sampler.predict(
                prompt=prompt,
                name=name,
                size=args.video_size,
                seed=seed,
                pixel_value_llava=pixel_value_llava,
                uncond_pixel_value_llava=uncond_pixel_value_llava,
                ref_latents=ref_latents,
                uncond_ref_latents=uncond_ref_latents,
                bg_latents=bg_latents,
                audio_prompts=audio_prompts,
                audio_strength=args.audio_strength,
                video_length=args.sample_n_frames,
                guidance_scale=args.cfg_scale,
                num_images_per_prompt=args.num_images,
                negative_prompt=negative_prompt,
                infer_steps=args.infer_steps,
                flow_shift=args.flow_shift_eval_video,
                use_linear_quadratic_schedule=args.use_linear_quadratic_schedule,
                linear_schedule_end=args.linear_schedule_end,
                use_deepcache=args.use_deepcache,
        )

        if rank == 0:
            samples = outputs['samples']
            for i, sample in enumerate(samples):
                sample = samples[i].unsqueeze(0)
                out_path = f"{save_path}/{save_name}.mp4"
                save_videos_grid(sample, out_path, fps=args.fps)
                if args.audio_condition and audio_path is not None:
                    out_audio_path = f"{save_path}/{save_name}_audio.mp4"
                    os.system(f"ffmpeg -i '{out_path}' -i '{audio_path}' -shortest '{out_audio_path}' -y -loglevel quiet; rm '{out_path}'")
                    logger.info(f'Sample save to: {out_audio_path}')
                else:
                    logger.info(f'Sample save to: {out_path}')
    
if __name__ == "__main__":
    main()
    
    
    
    
    
    
    
