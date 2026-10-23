"""Recorded from the live GitHub API on 2026-10-07 (releases/latest of each repo), trimmed to the
fields Snag reads and to the assets it uses plus a few neighbours. The checksum files are verbatim,
kept as Python literals so Git's line-ending conversion can't change them (Deno's has CRLF endings).

What the recording showed:
- yt-dlp/yt-dlp: the tag is the version yt-dlp prints for --version; SHA2-256SUMS is "<hash>  <name>".
- yt-dlp/FFmpeg-Builds: the tag is always "latest"; checksums.sha256 is "<hash>  <name>"; the zip holds
  ffmpeg-master-latest-win64-gpl/bin/{ffmpeg,ffprobe,ffplay}.exe (listing below).
- denoland/deno: deno-x86_64-pc-windows-msvc.zip.sha256sum is NOT "<hash>  <name>" but PowerShell's
  `Get-FileHash | Format-List` output, with the hash in upper case and the builder's path.
- Every asset also carries a "digest" ("sha256:<hex>"); it matches the checksum files."""
from __future__ import annotations

YTDLP_RELEASE = {
    "tag_name": "2026.08.19",
    "name": "yt-dlp 2026.08.19",
    "assets": [
        {
            "name": "SHA2-256SUMS",
            "size": 1595,
            "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/SHA2-256SUMS",
            "digest": "sha256:a63701f30755cb4d9317950d69703e9d751d490cb1b7059e1bb501a353fe7dcb",
        },
        {
            "name": "SHA2-256SUMS.sig",
            "size": 566,
            "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/SHA2-256SUMS.sig",
            "digest": "sha256:8d6f9a7c84c57222e3650c61699185878a3468308de702993ce4f20b5f9cf199",
        },
        {
            "name": "yt-dlp.exe",
            "size": 17840399,
            "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp.exe",
            "digest": "sha256:66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a",
        },
        {
            "name": "yt-dlp_x86.exe",
            "size": 13199160,
            "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp_x86.exe",
            "digest": "sha256:a8f91bd41452506bc81ebd2f369b186fea0ee7075413ba00cef9fd346a0a5d0c",
        },
    ],
}

FFMPEG_RELEASE = {
    "tag_name": "latest",
    "name": "Latest Auto-Build (2026-10-06 19:26)",
    "assets": [
        {
            "name": "checksums.sha256",
            "size": 848,
            "browser_download_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256",
            "digest": "sha256:858dce82df4e82109388893c4271e9dafe702ac76656c22ede4bd6a8cb5aa5af",
        },
        {
            "name": "ffmpeg-master-latest-win64-gpl-shared.zip",
            "size": 88815040,
            "browser_download_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
            "digest": "sha256:7ac7955cb58e8177a48bd65a615cea93d31890ccc1910718f22bcec2853109fe",
        },
        {
            "name": "ffmpeg-master-latest-win64-gpl.zip",
            "size": 200215042,
            "browser_download_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
            "digest": "sha256:944571bdb7307bb769ba1e526aea851447ea5be74e9758d2b4ad0e9c3a7e37a6",
        },
    ],
}

DENO_RELEASE = {
    "tag_name": "v2.9.7",
    "name": "v2.9.7",
    "assets": [
        {
            "name": "deno-x86_64-pc-windows-msvc.sha256sum",
            "size": 154,
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.sha256sum",
            "digest": "sha256:c9c50be24ea4746db14ee175ef510095f6c558a90e7e4bc3caf651aa37570e87",
        },
        {
            "name": "deno-x86_64-pc-windows-msvc.zip",
            "size": 42630221,
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.zip",
            "digest": "sha256:a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238",
        },
        {
            "name": "deno-x86_64-pc-windows-msvc.zip.sha256sum",
            "size": 177,
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.zip.sha256sum",
            "digest": "sha256:05f3e85ba3c6fdd78c15444fa0611c4957c2333693a71624bd33e226495e3f61",
        },
        {
            "name": "denort-x86_64-pc-windows-msvc.zip",
            "size": 32692172,
            "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.9.7/denort-x86_64-pc-windows-msvc.zip",
            "digest": "sha256:1ed45440e881cbadbfa754c8dd25c70635bf3b16a5ad663f65ec3c5cc4a975fd",
        },
    ],
}

YTDLP_SHA2_256SUMS = (
    "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6  yt-dlp\n"
    "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a  yt-dlp.exe\n"
    "072aad4f2a7604e92155f61a275a4752dc64046c8f6d90df3710525d94cd37c1  yt-dlp.tar.gz\n"
    "05b438997bafc3affdfda9d041353c9d73e04dc842207254b655b0887c4445b0  yt-dlp_arm64.exe\n"
    "58162f9bfdc27458ea47bfcb311cf47028f17d8154a8bf7d689861d46399230a  yt-dlp_linux\n"
    "32e72032766bef9199d99d15beb69fd52e46df8f8b06f0d8745db59e04d339e9  yt-dlp_linux.zip\n"
    "b16e4dab368a816cd05d477d698a605a6ae87ccee1c8ffd38fa21d7254141fcc  yt-dlp_linux_aarch64\n"
    "4e27ad43f3a34bacffd078694eb3edbb4e3b378e7da44edab2be02e98555516e  yt-dlp_linux_aarch64.zip\n"
    "bd51eb5fed7788008f4f30d281a182376a1297910b6c78e747403e757a546508  yt-dlp_linux_armv7l.zip\n"
    "0f192b7ec147ab6288885d6351d9ab67367640029b4377576ef46dd79cf7b202  yt-dlp_macos\n"
    "07e54b0865303c864006925913bce2604f8ee8cc6f18699bac9c309f9328a6d8  yt-dlp_macos.zip\n"
    "f3dec9cfeaf304cec98290fe41c6ad465d4b747d302473559643e7af24929722  yt-dlp_musllinux\n"
    "a8a9761628be455368db73ab54a2858da22342084f300f7e0d50bdcbef2acf82  yt-dlp_musllinux.zip\n"
    "17b164c4d258be92bb1ad146cb7c336b783aedb380814aabbcb7d52937f77e57  yt-dlp_musllinux_aarch64\n"
    "46087f4965f48612d056c6e4b00a0898e8230ba90d709a0a751c7cc5735a1da3  yt-dlp_musllinux_aarch64.zip\n"
    "30b4c14aafab6082becff7881e41b76df46dc43ea7633479410a91e29da492bf  yt-dlp_win.zip\n"
    "2ad49db7429a6204721f711a44199a8c9fa8d5df2183dd7fedc4ef6c9e2e50d3  yt-dlp_win_arm64.zip\n"
    "dc7c11f014af67f23d9fd989868048abedbce01709ff642a29eb3f7578b802f7  yt-dlp_win_x86.zip\n"
    "a8f91bd41452506bc81ebd2f369b186fea0ee7075413ba00cef9fd346a0a5d0c  yt-dlp_x86.exe\n"
)

FFMPEG_CHECKSUMS_SHA256 = (
    "ed366bcb71a3fa812a78b9c9fa1f29162ef4c7f2112545a4ecab3f802e79d497  ffmpeg-master-latest-win32-gpl-shared.zip\n"
    "d3eefa05c8f3e87a0a5e90bbcae532027561982b849c5ce8b1ed114687843b94  ffmpeg-master-latest-win32-gpl.zip\n"
    "7ac7955cb58e8177a48bd65a615cea93d31890ccc1910718f22bcec2853109fe  ffmpeg-master-latest-win64-gpl-shared.zip\n"
    "944571bdb7307bb769ba1e526aea851447ea5be74e9758d2b4ad0e9c3a7e37a6  ffmpeg-master-latest-win64-gpl.zip\n"
    "2638d9151b0ec449807f85b7067169f9e8fa637242542a7bce0ecb005ecb3e29  ffmpeg-master-latest-winarm64-gpl-shared.zip\n"
    "4b60310ec5d30491a9dc5d230ba071a1761e53db54d88c1b38db44046a249473  ffmpeg-master-latest-winarm64-gpl.zip\n"
    "be3d193bf1019afd99f84da96dc1b97575931abfbc8e242d4e81d5d8f3b74434  ffmpeg-master-latest-linux64-gpl.tar.xz\n"
    "79e859c0fe84079e7de4237c45d8f1e7f5d671988503f654632695f66166e355  ffmpeg-master-latest-linuxarm64-gpl.tar.xz\n"
)

DENO_ZIP_SHA256SUM = (
    "\r\n"
    "Algorithm : SHA256\r\n"
    "Hash      : A0C3101B4158D1DFB7D6A78A7BF0F3DE80C96BB423C152BEEC8BEB22786F2238\r\n"
    "Path      : C:\\a\\deno\\deno\\target\\release\\deno-x86_64-pc-windows-msvc.zip\r\n"
    "\r\n"
)

# Member names of the real zips (read from their central directories with ranged requests), without
# ffmpeg's doc/ tree. Directories end in "/".
FFMPEG_ZIP_MEMBERS = (
    "ffmpeg-master-latest-win64-gpl/",
    "ffmpeg-master-latest-win64-gpl/presets/",
    "ffmpeg-master-latest-win64-gpl/presets/libvpx-1080p.ffpreset",
    "ffmpeg-master-latest-win64-gpl/presets/libvpx-360p.ffpreset",
    "ffmpeg-master-latest-win64-gpl/LICENSE.txt",
    "ffmpeg-master-latest-win64-gpl/bin/",
    "ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe",
    "ffmpeg-master-latest-win64-gpl/bin/ffprobe.exe",
    "ffmpeg-master-latest-win64-gpl/bin/ffplay.exe",
)
DENO_ZIP_MEMBERS = ("deno.exe",)

# GitHub's reply when the unauthenticated limit (60 requests an hour) is used up: status 403 (429 for
# the secondary limits) with X-RateLimit-Remaining: 0 and X-RateLimit-Reset: <epoch seconds>. The
# header names were recorded from a live reply; this body follows GitHub's documented wording (using the
# limit up just to record it would block the machine's update checks for an hour).
RATE_LIMITED_BODY = {
    "message": "API rate limit exceeded for 203.0.113.7. (But here's the good news: Authenticated requests get a "
    "higher rate limit. Check out the documentation for more details.)",
    "documentation_url": "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting",
}
