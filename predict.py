import sys

sys.path.append("src/clip")
sys.path.append("src/blip")

import os
import hashlib
import math
import numpy as np
import pickle
from tqdm import tqdm
from PIL import Image
import torch
from torchvision import transforms
from torchvision.transforms.functional import InterpolationMode
import clip
from models.blip import blip_decoder
from cog import BasePredictor, Input, Path


DATA_PATH = "data"
chunk_size = 2048
flavor_intermediate_count = 2048
blip_image_eval_size = 384


class Predictor(BasePredictor):
    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""

        self.device = "cuda:0"

        print("Loading BLIP model...")
        self.blip_model = blip_decoder(
            pretrained="weights/model_large_caption.pth",  # downloaded with wget https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_large_caption.pth
            image_size=blip_image_eval_size,
            vit="large",
            med_config="src/blip/configs/med_config.json",
        )
        self.blip_model.eval()
        self.blip_model = self.blip_model.to(self.device)

        print("Loading CLIP model...")
        self.clip_models, self.clip_preprocess = {}, {}
        for clip_model_name in [
            "ViT-B/32",
            "ViT-B/16",
            "ViT-L/14",
            "ViT-L/14@336px",
            "RN101",
            "RN50",
            "RN50x4",
            "RN50x16",
            "RN50x64",
        ]:
            (
                self.clip_models[clip_model_name],
                self.clip_preprocess[clip_model_name],
            ) = clip.load(clip_model_name, device=self.device)
            self.clip_models[clip_model_name].cuda().eval()

        sites = [
            "Artstation",
            "behance",
            "cg society",
            "cgsociety",
            "deviantart",
            "dribble",
            "flickr",
            "instagram",
            "pexels",
            "pinterest",
            "pixabay",
            "pixiv",
            "polycount",
            "reddit",
            "shutterstock",
            "tumblr",
            "unsplash",
            "zbrush central",
        ]
        self.trending_list = [site for site in sites]
        self.trending_list.extend(["trending on " + site for site in sites])
        self.trending_list.extend(["featured on " + site for site in sites])
        self.trending_list.extend([site + " contest winner" for site in sites])
        raw_artists = load_list(f"{DATA_PATH}/artists.txt")
        self.artists = [f"by {a}" for a in raw_artists]
        self.artists.extend([f"inspired by {a}" for a in raw_artists])

    def predict(
        self,
        image: Path = Input(description="Input image"),
        clip_model_name: str = Input(
            default="ViT-L/14",
            choices=[
                "ViT-B/32",
                "ViT-B/16",
                "ViT-L/14",
                "ViT-L/14@336px",
                "RN101",
                "RN50",
                "RN50x4",
                "RN50x16",
                "RN50x64",
            ],
            description="Choose a clip model.",
        ),
    ) -> str:
        """Run a single prediction on the model"""
        clip_model = self.clip_models[clip_model_name]
        clip_preprocess = self.clip_preprocess[clip_model_name]

        artists = LabelTable(self.artists, "artists", clip_model_name, clip_model)
        flavors = LabelTable(
            load_list(f"{DATA_PATH}/flavors.txt"),
            "flavors",
            clip_model_name,
            clip_model,
        )
        mediums = LabelTable(
            load_list(f"{DATA_PATH}/mediums.txt"),
            "mediums",
            clip_model_name,
            clip_model,
        )
        movements = LabelTable(
            load_list(f"{DATA_PATH}/movements.txt"),
            "movements",
            clip_model_name,
            clip_model,
        )
        trendings = LabelTable(
            self.trending_list, "trendings", clip_model_name, clip_model
        )

        image = Image.open(str(image)).convert("RGB")

        labels = [flavors, mediums, artists, trendings, movements]

        prompt = interrogate(
            image,
            clip_model_name,
            clip_preprocess,
            clip_model,
            self.blip_model,
            *labels,
        )

        return prompt


class LabelTable:
    def __init__(self, labels, desc, clip_model_name, clip_model):
        self.labels = labels
        self.embeds = []

        hash = hashlib.sha256(",".join(labels).encode()).hexdigest()

        os.makedirs("./cache", exist_ok=True)
        cache_filepath = f"./cache/{desc}.pkl"
        if desc is not None and os.path.exists(cache_filepath):
            with open(cache_filepath, "rb") as f:
                data = pickle.load(f)
                if data.get("hash") == hash and data.get("model") == clip_model_name:
                    self.labels = data["labels"]
                    self.embeds = data["embeds"]

        if len(self.labels) != len(self.embeds):
            self.embeds = []
            chunks = np.array_split(self.labels, max(1, len(self.labels) / chunk_size))
            for chunk in tqdm(chunks, desc=f"Preprocessing {desc}" if desc else None):
                text_tokens = clip.tokenize(chunk).cuda()
                with torch.no_grad():
                    text_features = clip_model.encode_text(text_tokens).float()
                text_features /= text_features.norm(dim=-1, keepdim=True)
                text_features = text_features.half().cpu().numpy()
                for i in range(text_features.shape[0]):
                    self.embeds.append(text_features[i])

            with open(cache_filepath, "wb") as f:
                pickle.dump(
                    {
                        "labels": self.labels,
                        "embeds": self.embeds,
                        "hash": hash,
                        "model": clip_model_name,
                    },
                    f,
                )

    def _rank(self, image_features, text_embeds, device="cuda", top_count=1):
        top_count = min(top_count, len(text_embeds))
        similarity = torch.zeros((1, len(text_embeds))).to(device)
        text_embeds = (
            torch.stack([torch.from_numpy(t) for t in text_embeds]).float().to(device)
        )
        for i in range(image_features.shape[0]):
            similarity += (image_features[i].unsqueeze(0) @ text_embeds.T).softmax(
                dim=-1
            )
        _, top_labels = similarity.cpu().topk(top_count, dim=-1)
        return [top_labels[0][i].numpy() for i in range(top_count)]

    def rank(self, image_features, top_count=1):
        if len(self.labels) <= chunk_size:
            tops = self._rank(image_features, self.embeds, top_count=top_count)
            return [self.labels[i] for i in tops]

        num_chunks = int(math.ceil(len(self.labels) / chunk_size))
        keep_per_chunk = int(chunk_size / num_chunks)

        top_labels, top_embeds = [], []
        for chunk_idx in tqdm(range(num_chunks)):
            start = chunk_idx * chunk_size
            stop = min(start + chunk_size, len(self.embeds))
            tops = self._rank(
                image_features, self.embeds[start:stop], top_count=keep_per_chunk
            )
            top_labels.extend([self.labels[start + i] for i in tops])
            top_embeds.extend([self.embeds[start + i] for i in tops])

        tops = self._rank(image_features, top_embeds, top_count=top_count)
        return [top_labels[i] for i in tops]


def generate_caption(pil_image, blip_model, device="cuda"):
    gpu_image = (
        transforms.Compose(
            [
                transforms.Resize(
                    (blip_image_eval_size, blip_image_eval_size),
                    interpolation=InterpolationMode.BICUBIC,
                ),
                transforms.ToTensor(),
                transforms.Normalize(
                    (0.48145466, 0.4578275, 0.40821073),
                    (0.26862954, 0.26130258, 0.27577711),
                ),
            ]
        )(pil_image)
        .unsqueeze(0)
        .to(device)
    )

    with torch.no_grad():
        caption = blip_model.generate(
            gpu_image, sample=False, num_beams=3, max_length=20, min_length=5
        )
    return caption[0]


def rank_top(image_features, text_array, clip_model, device="cuda"):
    text_tokens = clip.tokenize([text for text in text_array]).cuda()
    with torch.no_grad():
        text_features = clip_model.encode_text(text_tokens).float()
    text_features /= text_features.norm(dim=-1, keepdim=True)

    similarity = torch.zeros((1, len(text_array)), device=device)
    for i in range(image_features.shape[0]):
        similarity += (image_features[i].unsqueeze(0) @ text_features.T).softmax(dim=-1)

    _, top_labels = similarity.cpu().topk(1, dim=-1)
    return text_array[top_labels[0][0].numpy()]


def similarity(image_features, text, clip_model):
    text_tokens = clip.tokenize([text]).cuda()
    with torch.no_grad():
        text_features = clip_model.encode_text(text_tokens).float()
    text_features /= text_features.norm(dim=-1, keepdim=True)
    similarity = text_features.cpu().numpy() @ image_features.cpu().numpy().T
    return similarity[0][0]


def load_list(filename):
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        items = [line.strip() for line in f.readlines()]
    return items


def interrogate(image, clip_model_name, clip_preprocess, clip_model, blip_model, *args):
    flavors, mediums, artists, trendings, movements = args
    caption = generate_caption(image, blip_model)

    images = clip_preprocess(image).unsqueeze(0).cuda()
    with torch.no_grad():
        image_features = clip_model.encode_image(images).float()
    image_features /= image_features.norm(dim=-1, keepdim=True)

    flaves = flavors.rank(image_features, flavor_intermediate_count)
    best_medium = mediums.rank(image_features, 1)[0]
    best_artist = artists.rank(image_features, 1)[0]
    best_trending = trendings.rank(image_features, 1)[0]
    best_movement = movements.rank(image_features, 1)[0]

    best_prompt = caption
    best_sim = similarity(image_features, best_prompt, clip_model)

    def check(addition):
        nonlocal best_prompt, best_sim
        prompt = best_prompt + ", " + addition
        sim = similarity(image_features, prompt, clip_model)
        if sim > best_sim:
            best_sim = sim
            best_prompt = prompt
            return True
        return False

    def check_multi_batch(opts):
        nonlocal best_prompt, best_sim
        prompts = []
        for i in range(2 ** len(opts)):
            prompt = best_prompt
            for bit in range(len(opts)):
                if i & (1 << bit):
                    prompt += ", " + opts[bit]
            prompts.append(prompt)

        t = LabelTable(prompts, None, clip_model_name, clip_model)
        best_prompt = t.rank(image_features, 1)[0]
        best_sim = similarity(image_features, best_prompt, clip_model)

    check_multi_batch([best_medium, best_artist, best_trending, best_movement])

    extended_flavors = set(flaves)
    for _ in tqdm(range(25), desc="Flavor chain"):
        try:
            best = rank_top(
                image_features,
                [f"{best_prompt}, {f}" for f in extended_flavors],
                clip_model,
            )
            flave = best[len(best_prompt) + 2 :]
            if not check(flave):
                break
            extended_flavors.remove(flave)
        except:
            # exceeded max prompt length
            break

    return best_prompt
