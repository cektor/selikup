from setuptools import setup, find_packages

setup(
    name="selikup",  # Paket adı
    version="1.0",  # Paket sürümü
    description="This program helps you download and install the kernel from the KERNEL main repository.",  # Paket açıklaması
    author="Fatih Önder",  # Paket sahibi adı
    author_email="fatih@algyazilim.com",  # Paket sahibi e-posta adresi
    url="https://github.com/cektor/selikup",  # Paket deposu URL'si
    packages=find_packages(),  # Otomatik olarak tüm alt paketleri bulur
    python_requires='>=3.6',  # Python sürüm gereksinimi
    install_requires=[
        'beautifulsoup4',  # Debian: python3-bs4
        'PyQt5',           # Debian: python3-pyqt5
        'requests',        # Debian: python3-requests
        'certifi',         # Debian: python3-certifi
        'chardet',         # Debian: python3-chardet
        'idna',            # Debian: python3-idna
        'urllib3',         # Debian: python3-urllib3
    ],
    package_data={
        'selikup': ['*.png', '*.desktop'],  # Paket içindeki ekstra dosyalar
    },
    data_files=[
        ('share/applications', ['selikup.desktop']),  # Uygulama menüsüne .desktop dosyası
        ('share/icons/hicolor/48x48/apps', ['selikup001.png']),  # Simge dosyası
    ],
    entry_points={
        'gui_scripts': [
            'selikup=selikup:main',  # Komut satırından veya GUI başlatıldığında çalışacak fonksiyon
        ]
    },
)

