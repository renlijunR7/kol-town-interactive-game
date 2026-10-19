#!/usr/bin/env python3
"""从小镇底图中抠出渲染好的居民作为 NPC 精灵，并生成原位补丁后的底图副本。

用法：python3 tools/extract_npc_sprites.py
输出：assets/generated/npc/pN.png（透明底精灵）、assets/generated/luna-town-citybuilder-v3-npc.png（补丁底图）
加人物：在 PEOPLE 里追加 (名字, (x0,y0,x1,y1) 原图像素框, 补丁水平偏移 dx)，再到 game.js 的 TOWN_NPCS 登记 w/h。
"""
from PIL import Image, ImageFilter, ImageDraw
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'assets/generated/luna-town-citybuilder-v3.png'
OUT_DIR = ROOT / 'assets/generated/npc'
OUT_BASE = ROOT / 'assets/generated/luna-town-citybuilder-v3-npc.png'
PEOPLE = [
    ('p1', (316, 586, 338, 646), -24), ('p2', (333, 586, 355, 646), +24), ('p3', (663, 624, 694, 691), -34),
    ('p4', (114, 666, 136, 718), -24), ('p5', (149, 668, 173, 723), +26), ('p6', (702, 1004, 728, 1068), -30),
]

def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

def key_out(tile):
    """边框颜色作背景参考，从四边泛洪去背景，只保留最大的不透明连通块。"""
    w, h = tile.size; px = tile.load()
    def med(s): s = sorted(s, key=sum); return s[len(s) // 2]
    refs = [med([px[x, 0] for x in range(w)]), med([px[x, h - 1] for x in range(w)]),
            med([px[0, y] for y in range(h)]), med([px[w - 1, y] for y in range(h)])]
    is_bg = lambda c: min(dist(c, r) for r in refs) < 46
    alpha = [[255] * w for _ in range(h)]; seen = set(); q = deque()
    q.extend((x, y) for x in range(w) for y in (0, h - 1)); q.extend((x, y) for y in range(h) for x in (0, w - 1))
    while q:
        x, y = q.popleft()
        if (x, y) in seen or not (0 <= x < w and 0 <= y < h): continue
        seen.add((x, y))
        if not is_bg(px[x, y]): continue
        alpha[y][x] = 0; q.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])
    seen = set(); best = []
    for sy in range(h):
        for sx in range(w):
            if alpha[sy][sx] and (sx, sy) not in seen:
                comp = []; q = deque([(sx, sy)]); seen.add((sx, sy))
                while q:
                    x, y = q.popleft(); comp.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < w and 0 <= ny < h and alpha[ny][nx] and (nx, ny) not in seen:
                            seen.add((nx, ny)); q.append((nx, ny))
                if len(comp) > len(best): best = comp
    keep = set(best)
    out = tile.convert('RGBA'); po = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, _ = po[x, y]; po[x, y] = (r, g, b, 255 if (x, y) in keep else 0)
    out.putalpha(out.getchannel('A').filter(ImageFilter.GaussianBlur(0.5)))
    return out

def split_rig(sprite):
    """把精灵切成 body / leg-l / leg-r 三层（同尺寸，透明底），返回 (层, 髋线比例, 髋中心 x 比例)。"""
    w, h = sprite.size; a = sprite.getchannel('A').load()
    rows = [y for y in range(h) if any(a[x, y] > 40 for x in range(w))]
    top, bottom = rows[0], rows[-1]
    hip = top + int((bottom - top) * 0.56)          # 髋线：人物高度的 56%
    xs = [x for y in range(hip, bottom + 1) for x in range(w) if a[x, y] > 40]
    mid = sum(xs) / len(xs) if xs else w / 2          # 腿部像素重心作为左右腿分界
    body, legl, legr = (Image.new('RGBA', sprite.size, (0, 0, 0, 0)) for _ in range(3))
    px = sprite.load(); pb, pl, pr = body.load(), legl.load(), legr.load()
    for y in range(h):
        for x in range(w):
            c = px[x, y]
            if c[3] == 0: continue
            if y < hip: pb[x, y] = c
            elif x < mid: pl[x, y] = c
            else: pr[x, y] = c
    # 髋线处让上身多盖 2px，避免腿摆动时露缝
    for y in range(hip, min(h, hip + 2)):
        for x in range(w):
            if px[x, y][3]: pb[x, y] = px[x, y]
    return {'body': body, 'leg-l': legl, 'leg-r': legr}, hip / h, mid / w

def main():
    src = Image.open(SRC).convert('RGB'); base = src.copy(); OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, (x0, y0, x1, y1), dx in PEOPLE:
        sprite = key_out(src.crop((x0, y0, x1, y1))); sprite.save(OUT_DIR / f'{name}.png')
        layers, hip, mid = split_rig(sprite)
        for part, img in layers.items(): img.save(OUT_DIR / f'{name}-{part}.png')
        print(f'{name}: hip={hip:.3f} mid={mid:.3f}')
        m = 6; box = (x0 - m, y0 - m, x1 + m, y1 + m)
        patch = src.crop((box[0] + dx, box[1], box[2] + dx, box[3]))
        mask = Image.new('L', patch.size, 0)
        ImageDraw.Draw(mask).rectangle((m, m, patch.width - m, patch.height - m), fill=255)
        base.paste(patch, (box[0], box[1]), mask.filter(ImageFilter.GaussianBlur(3)))
        print(f'{name}: {x1 - x0}x{y1 - y0}')
    base.save(OUT_BASE, optimize=True)

if __name__ == '__main__':
    main()
