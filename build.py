#!/usr/bin/env python3

import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

# Keep original headers
headers = {
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
}


def download_progress_hook(count, blocksize, totalsize):
    if totalsize > 0:
        percent = min(int(count * blocksize * 100 / totalsize), 100)
        sys.stdout.write(f"\rDownloading... {percent}%")
        if percent == 100:
            sys.stdout.write("\n")
    sys.stdout.flush()


def apply_patch(product_path: str, patch_data: dict) -> None:
    with open(file=product_path, mode="r") as product_file:
        product_data = json.load(product_file)

    # Apply patches in memory
    for key in patch_data.keys():
        product_data[key] = patch_data[key]

    with open(file=product_path, mode="w") as product_file:
        json.dump(obj=product_data, fp=product_file, indent="\t")


# Get latest tag
try:
    latest_tag = (
        subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"], cwd=os.getcwd()
        )
        .decode()
        .strip()
    )
    print("latest_tag", latest_tag)
except subprocess.CalledProcessError:
    print("No git tags found, using '0.0.0' as fallback")
    latest_tag = "0.0.0"

# Check version from headers first
url = "https://windsurf-stable.codeium.com/api/update/linux-x64/stable/latest"
get_version_req = urllib.request.Request(url, method="GET", headers=headers)
with urllib.request.urlopen(get_version_req) as get_version_response:
    get_version_data = json.load(get_version_response)

download_url = get_version_data.get("url")
latest_version = get_version_data.get("windsurfVersion")

print("latest_version", latest_version)
if latest_version == latest_tag:
    print("No update needed")
    sys.exit(0)

# Set environment variables for GitHub Actions
with open(os.environ.get("GITHUB_ENV", os.devnull), "a") as f:
    f.write("APP_UPDATE_NEEDED=true\n")
    f.write(f"VERSION={latest_version}\n")

os.environ["APPIMAGE_EXTRACT_AND_RUN"] = "1"

# Handle Cursor tar.gz download and extraction
with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp_file:
    opener = urllib.request.build_opener()
    opener.addheaders = list(headers.items())
    urllib.request.install_opener(opener)
    print("Downloading Windsurf tar.gz...")
    urllib.request.urlretrieve(download_url, tmp_file.name, download_progress_hook)
    tmp_file.flush()
    os.fsync(tmp_file.fileno())
    tmp_name = tmp_file.name

shutil.rmtree("windsurf.AppDir", ignore_errors=True)

# Create extraction directory
os.makedirs("windsurf.AppDir", exist_ok=True)

# Extract tar.gz file using Python's built-in tarfile module
with tarfile.open(tmp_name, "r:gz") as tar:
    tar.extractall(path="windsurf.AppDir", filter="fully_trusted")

# Clean up after extraction is complete
try:
    os.unlink(tmp_name)
except OSError:
    print(f"Warning: Could not remove temporary file {tmp_name}")

shutil.copyfile("windsurf.desktop", "windsurf.AppDir/windsurf.desktop")
shutil.copyfile("AppRun", "windsurf.AppDir/AppRun")
os.chmod("windsurf.AppDir/AppRun", 0o755)
shutil.copyfile(
    "windsurf.AppDir/Windsurf/resources/app/resources/linux/code.png",
    "windsurf.AppDir/windsurf.png",
)

# Handle appimagetool and patches in separate temp directory
with tempfile.TemporaryDirectory() as tools_tmpdir:
    machine = platform.machine()
    original_dir = os.getcwd()

    # Download and setup appimagetool
    appimagetool_url = f"https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-{machine}.AppImage"
    appimagetool_path = os.path.join(tools_tmpdir, "appimagetool")
    print("Downloading appimagetool...")
    urllib.request.urlretrieve(
        appimagetool_url, appimagetool_path, download_progress_hook
    )
    os.chmod(appimagetool_path, 0o755)

    # Extract appimagetool
    subprocess.run(
        [appimagetool_path, "--appimage-extract"],
        check=True,
        cwd=tools_tmpdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    appimagetool_dir = os.path.join(tools_tmpdir, "squashfs-root")

    # Set permissions
    os.chmod("windsurf.AppDir", 0o755)

    # Download and apply patches
    patch_urls = {
        "features": "https://aur.archlinux.org/cgit/aur.git/plain/patch.json?h=windsurf-features",
        # "marketplace": "https://aur.archlinux.org/cgit/aur.git/plain/patch.json?h=code-marketplace",
    }

    for patch_url in patch_urls.values():
        req = urllib.request.Request(patch_url, headers=headers)
        with urllib.request.urlopen(req) as response:
            patch_data = json.load(response)
            apply_patch(
                "windsurf.AppDir/Windsurf/resources/app/product.json",
                patch_data,
            )
    
    marketplace_patch = {
        "serviceUrl": "https://marketplace.visualstudio.com/_apis/public/gallery",
        "cacheUrl": "https://vscode.blob.core.windows.net/gallery/index",
        "itemUrl": "https://marketplace.visualstudio.com/items"
    }

    with open("windsurf.AppDir/Windsurf/resources/app/product.json", "r") as product_file:
        product_data = json.load(product_file)

    # Apply marketplace patch
    for key in marketplace_patch.keys():
        product_data[key] = marketplace_patch[key]
    product_data.pop("linkProtectionTrustedDomains", None)

    with open("windsurf.AppDir/Windsurf/resources/app/product.json", "w") as product_file:
        json.dump(product_data, product_file, indent="\t")

    # Build final AppImage
    # Create dist directory with absolute path
    dist_dir = os.path.join(original_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    github_repo = os.environ.get("GITHUB_REPOSITORY", "").replace("/", "|")
    update_info = f"gh-releases-zsync|{github_repo}|latest|Windsurf*.AppImage.zsync"
    output_name = f"Windsurf-{latest_version}-{machine}.AppImage"

    # Run appimagetool to create the AppImage
    subprocess.run(
        [
            os.path.join(appimagetool_dir, "AppRun"),
            "-n",
            "--comp",
            "zstd",
            os.path.join(original_dir, "windsurf.AppDir"),
            "--updateinformation",
            update_info,
            output_name,
        ],
        check=True,
    )

for root, _, files in os.walk(pathlib.Path.home()):
    for file in files:
        if file.startswith(f"Windsurf-{latest_version}-{machine}"):
            src = os.path.join(root, file)
            dst = os.path.join(dist_dir, file)
            shutil.move(src, dst)
