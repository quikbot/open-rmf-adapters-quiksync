from setuptools import find_packages, setup

PACKAGE_NAME = "door_adapter_quiksync"

setup(
    name=PACKAGE_NAME,
    version="0.1.0",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/door_adapter_quiksync"]),
        ("share/door_adapter_quiksync", ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
        "quiksync_client",
    ],
    zip_safe=True,
    maintainer="QuikBot",
    maintainer_email="tech@quikbot.ai",
    description="QuikSync Open-RMF door adapter — v1 stub; real implementation lands in v2.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "door_adapter_quiksync = door_adapter_quiksync.adapter:main",
        ],
    },
)
