from setuptools import setup

setup(
    name="PyMicroBot",
    packages=["microbot"],
    install_requires=["bleak>=0.19.0", "bleak-retry-connector>=1.4.0"],
    version="0.0.19",
    description="A library to communicate with MicroBot",
    author="spycle",
    url="https://github.com/spycle/pyMicroBot/",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Other Environment",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
