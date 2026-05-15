import os
from glob import glob

from setuptools import find_packages, setup

PACKAGE_NAME = "lift_adapter_quiksync"

setup(
    name=PACKAGE_NAME,
    version="0.2.1",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + PACKAGE_NAME]),
        (os.path.join("share", PACKAGE_NAME), ["package.xml"]),
        (os.path.join("share", PACKAGE_NAME, "config"), glob("config/*.example")),
        (os.path.join("share", PACKAGE_NAME, "launch"), glob("launch/*.launch.xml")),
    ],
    install_requires=[
        "setuptools",
        "quiksync_client",
        "pyyaml>=6",
    ],
    zip_safe=True,
    maintainer="QuikBot",
    maintainer_email="tech@quikbot.ai",
    description="QuikSync Open-RMF lift adapter — bridges QuikSync-managed lifts to a customer Open-RMF deployment.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "lift_adapter_quiksync = lift_adapter_quiksync.adapter:main",
        ],
    },
)
