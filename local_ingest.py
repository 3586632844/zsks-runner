#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地一键入库（抖音 / B站 / 小红书 通用）。

适用场景：GitHub 跑在美国机房 IP，抖音对数据中心 IP 一律风控（实测 Azure / Cloudflare 出口均被封），
而"你自己的本机"通常是 中国/住宅 IP（抖音放行）。因此抖音视频在你本机跑即可绕过风控，无需任何云/代理/服务器。

流程：提取元数据 → 下载音频 → 本地 whisper 转写 → DeepSeek 结构化 → 调 zsks 内部接口落库。
仅依赖两个项目既有密钥（放 .env，勿提交）：DEEPSEEK_KEY、INGEST_TOKEN。

用法：
  # 首次：安装依赖
  pip install faster-whisper requests
  # 系统还需装 ffmpeg（apt install ffmpeg / brew install ffmpeg）

  # 入库一个抖音链接（默认 small 模型转写；本机显存小可加 --model tiny/base）
  python3 local_ingest.py "https://v.douyin.com/xxxx/"

  # 不想转写（只存文案）：
  python3 local_ingest.py "https://v.douyin.com/xxxx/" --skip-transcribe
"""
import os, sys, json, uuid, argparse, urllib.request, urllib.error
from pathlib import Path

# ---- 载入 .env（不提交；仅本地用）----
_HERE = Path(__file__).resolve().parent
_ENVF = _HERE / '.env'
if _ENVF.exists():
    for _line in _ENVF.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---- 复用仓库既有的提取/转写/结构化逻辑 ----
sys.path.insert(0, str(_HERE / 'scripts'))
from extractors import extract_douyin, extract_bilibili, extract_xiaohongshu
from common import download_audio, transcribe, structure_note, post_ingest, log


def pick_extractor(url):
    u = url.lower()
    if 'douyin.com' in u or 'iesdouyin' in u:
        return 'douyin', extract_douyin
    if 'bilibili.com' in u or 'b23.tv' in u:
        return 'bilibili', extract_bilibili
    if 'xiaohongshu.com' in u or 'xhslink.com' in u:
        return 'xiaohongshu', extract_xiaohongshu
    return None, None


def main():
    ap = argparse.ArgumentParser(description='本地一键入库（绕过抖音数据中心IP风控）')
    ap.add_argument('url', help='作品链接（抖音/B站/小红书）')
    ap.add_argument('--model', default=os.environ.get('WHISPER_MODEL', 'small'),
                    help='whisper 模型 tiny/base/small（本机弱可换 tiny）')
    ap.add_argument('--skip-transcribe', action='store_true', help='不转写，仅存文案/正文')
    ap.add_argument('--worker', default=os.environ.get('WORKER_URL', 'https://zsks.pages.dev'),
                    help='zsks Worker 地址')
    args = ap.parse_args()

    deepseek_key = os.environ.get('DEEPSEEK_KEY')
    ingest_token = os.environ.get('INGEST_TOKEN')
    if not deepseek_key:
        sys.exit('✗ 缺少 DEEPSEEK_KEY（写入 .env 或环境变量）')
    if not ingest_token:
        sys.exit('✗ 缺少 INGEST_TOKEN（GitHub 仓库 Settings→Secrets 里的值，复制到 .env）')
    os.environ['DEEPSEEK_KEY'] = deepseek_key  # 供 structure_note 使用

    plat, extractor = pick_extractor(args.url)
    if not extractor:
        sys.exit('✗ 不支持的链接（仅抖音/B站/小红书）')
    log(f'本地提取[{plat}]：{args.url}')
    info, caption, images = extractor(args.url)
    if not info:
        sys.exit('✗ 提取失败（本机若也是机房/海外IP可能被风控；确认本机为中国/住宅网络）')
    external_id = info.get('external_id') or ''
    name = info.get('nickname') or ''
    log(f'  作者={name} external_id={external_id} 文案长={len(caption)}')

    # ---- 下载音频 + 本地转写 ----
    transcript = ''
    if not args.skip_transcribe:
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix='zsksloc_')
        try:
            audio = None
            if info.get('play_url'):
                audio = download_audio(info['play_url'], tmp, referer='https://www.douyin.com/')
            if not audio:
                audio = download_audio(args.url, tmp, referer='https://www.douyin.com/')
            if audio:
                transcript = transcribe(audio, args.model)
                log(f'  转写字数={len(transcript)}')
            else:
                log('  音频下载失败，仅用文案')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        log('  --skip-transcribe：跳过转写')

    # ---- DeepSeek 结构化 ----
    log('  AI 结构化…')
    note, title = structure_note(caption, transcript, '', info)
    log(f'  摘要={note.get("summary")} 大纲={note.get("outline")}')

    # ---- 组装 payload，调 zsks 内部接口落库 ----
    job_id = str(uuid.uuid4())
    payload = {
        'job_id': job_id,
        'ok': True,
        'blogger': {'platform': plat, 'name': name, 'uid': info.get('uid') or ''},
        'post': {
            'platform': plat,
            'content_type': info.get('content_type', 'video'),
            'external_id': external_id,
            'url': info.get('url') or args.url,
            'title': title,
            'publish_time': int(info.get('publish_time') or 0),
            'category': note.get('category', '其他'),
            'cover': info.get('cover') or '',
            'play_url': info.get('play_url') or '',
            'desc_text': (info.get('desc') or '')[:500],
            'has_text': bool(caption),
            'has_transcript': bool(transcript),
            'has_ocr': bool(images),
        },
        'note': {
            'caption': caption,
            'transcript': transcript,
            'ocr_text': '',
            'summary': note.get('summary', ''),
            'outline': note.get('outline', []),
            'quotes': note.get('quotes', []),
            'tags': note.get('tags', []),
        },
    }
    os.environ['WORKER_URL'] = args.worker.rstrip('/')
    os.environ['INGEST_TOKEN'] = ingest_token
    st, body = post_ingest(payload)
    try:
        resp = json.loads(body) if isinstance(body, str) else {}
    except Exception:
        resp = {}
    if st == 200 and resp.get('ok'):
        print(f'✅ 入库成功 post_id={resp.get("post_id")}（前端即可查看；若 external_id 已存在则更新）')
    elif st == 401:
        sys.exit('✗ 落库接口 401：INGEST_TOKEN 不正确（请核对 GitHub 仓库 Secrets 中的值）')
    else:
        sys.exit(f'✗ 落库接口返回 {st}：{body[:300]}')


if __name__ == '__main__':
    main()
