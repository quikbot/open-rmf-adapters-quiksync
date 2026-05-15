from setuptools import find_packages, setup

PACKAGE_NAME = "quiksync_client"

setup(
    name=PACKAGE_NAME,
    version="0.2.1",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/quiksync_client"]),
        ("share/quiksync_client", ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
        "httpx>=0.27",
        "websockets>=12",
        "pydantic>=2.5",
    ],
    zip_safe=True,
    maintainer="QuikBot",
    maintainer_email="tech@quikbot.ai",
    description=(
        "HTTPS + WSS client for the QuikSync Open-RMF adapter API. "
        "Shared core used by the fleet, door, and lift adapter packages."
    ),
    license="Apache-2.0",
    tests_require=["pytest"],
)
