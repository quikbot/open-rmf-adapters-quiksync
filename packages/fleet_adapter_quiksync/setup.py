import os
from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "fleet_adapter_quiksync"

setup(
    name=PACKAGE_NAME,
    version="0.2.0",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + PACKAGE_NAME]),
        (os.path.join("share", PACKAGE_NAME), ["package.xml"]),
        (os.path.join("share", PACKAGE_NAME, "launch"), glob("launch/*.launch.xml")),
        (os.path.join("share", PACKAGE_NAME, "config"), glob("config/*.example")),
    ],
    install_requires=[
        "setuptools",
        "quiksync_client",
        "pyyaml>=6",
    ],
    zip_safe=True,
    maintainer="QuikBot",
    maintainer_email="tech@quikbot.ai",
    description="QuikSync Open-RMF fleet adapter — registers QuikSync-managed fleets with the customer's Open-RMF deployment via EasyFullControl.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "fleet_adapter_quiksync = fleet_adapter_quiksync.adapter:main",
        ],
    },
)
