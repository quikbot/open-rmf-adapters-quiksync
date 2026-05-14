from setuptools import find_packages, setup

PACKAGE_NAME = "lift_adapter_quiksync"

setup(
    name=PACKAGE_NAME,
    version="0.1.2",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/lift_adapter_quiksync"]),
        ("share/lift_adapter_quiksync", ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
        "quiksync_client",
    ],
    zip_safe=True,
    maintainer="QuikBot",
    maintainer_email="tech@quikbot.ai",
    description="QuikSync Open-RMF lift adapter — v1 stub; real implementation lands in v2.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "lift_adapter_quiksync = lift_adapter_quiksync.adapter:main",
        ],
    },
)
