from setuptools import setup, find_packages

setup(
    name="deva-client",
    version="0.1.0",
    description="设备A GPU 算力远程调度 CLI",
    packages=find_packages(),
    python_requires=">=3.9",
    entry_points={"console_scripts": ["deva=deva_client.cli:main"]},
)
