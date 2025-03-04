import setuptools

setuptools.setup(
    name="linak-mqtt-ctrl",
    version="0.1.0",
    description="A utility to interact with USB2LIN06 device asynchronously using libusb1 and gmqtt.",
    author="Ryan Orth",
    author_email="giantorth@gmail.com",
    url="https://github.com/giantorth/linak-mqtt-ctrl",
    packages=setuptools.find_packages(),
    install_requires=[
        "gmqtt>=0.6.8",
        "libusb1>=1.9.3",
        "pyyaml"
    ],
    entry_points={
        'console_scripts': [
            'linak-mqtt-ctrl=linak_mqtt_ctrl.__init__:main',
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.7',
    setup_requires=['pbr>=2.0.0'],
    pbr=True,
)
