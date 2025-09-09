#!/usr/bin/env python3
import sys
import os
import requests
import json
from bs4 import BeautifulSoup
import subprocess
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QPushButton, QLabel, QComboBox, 
                           QProgressBar, QMessageBox, QTextEdit, QScrollArea,
                           QTabWidget, QTreeWidget, QTreeWidgetItem, QDialog,
                           QCheckBox, QSpinBox, QLineEdit, QFileDialog, QMenu,
                           QSystemTrayIcon, QStatusBar)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer
from PyQt5.QtGui import QPalette, QColor, QIcon, QFont, QPixmap
import re
import tempfile
import shutil
import time

# helper: yükseltme komutu oluştur
def escalate_cmd(cmd):
    """Return a command list that will execute cmd with elevated privileges.
    If cmd is a single script path (list length 1), wrap it so pkexec/sudo run the script with /bin/sh and necessary env vars preserved for polkit GUI.
    """
    # Eğer pkexec varsa, pkexec env DISPLAY=... XAUTHORITY=... /bin/sh script
    if shutil.which('pkexec'):
        display = os.environ.get('DISPLAY', '')
        xauth = os.environ.get('XAUTHORITY', '')
        xdg = os.environ.get('XDG_RUNTIME_DIR', '')
        env_parts = []
        if display:
            env_parts.append(f'DISPLAY={display}')
        if xauth:
            env_parts.append(f'XAUTHORITY={xauth}')
        if xdg:
            env_parts.append(f'XDG_RUNTIME_DIR={xdg}')
        if len(cmd) == 1 and os.path.isfile(cmd[0]):
            return ['pkexec', 'env'] + env_parts + ['/bin/sh', cmd[0]]
        return ['pkexec'] + cmd
    # sudo ile çalıştır
    if shutil.which('sudo'):
        if len(cmd) == 1 and os.path.isfile(cmd[0]):
            return ['sudo', '-E', '/bin/sh', cmd[0]]
        return ['sudo', '-E'] + cmd
    return cmd


def normalize_version_str(v):
    """Verilen sürüm string'inden sayısal version tuple döner. Örn: '6.16.4-...' -> (6,16,4)
    Eğer parça sayısı farklıysa eksik parçalar 0 ile tamamlanır."""
    if not v:
        return (0, 0, 0)
    v = str(v)
    # önce - sonrası parçaları kaldır
    core = v.split('-')[0]
    parts = core.split('.')
    nums = []
    for p in parts:
        # sadece başındaki sayı alınsın (örn '2precise' gibi ifadeler engellensin)
        m = re.match(r"^(\d+)", p)
        if m:
            nums.append(int(m.group(1)))
        else:
            # eğer sayı yoksa 0 ekle
            nums.append(0)
    # normalize uzunluk (major, minor, patch)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def version_key_variants(v):
    """Verilen sürüm string'inden eşleştirme amaçlı olası anahtar setini döndürür.
    Örneğin: '6.16.4-061604-generic' -> {'6.16.4', '6.16.4-061604', '6.16.4-061604-generic'}
    Bu, dpkg paket adları, /boot vmlinuz isimleri ve available listesi arasındaki farklı formatları köprüler.
    """
    if not v:
        return set()
    s = str(v).strip()
    keys = set()
    keys.add(s)
    if s.startswith('v'):
        keys.add(s[1:])
    core = s.split('-')[0]
    keys.add(core)
    nums = normalize_version_str(s)
    keys.add(f"{nums[0]}.{nums[1]}.{nums[2]}")
    parts = s.split('-')
    if len(parts) >= 2:
        keys.add(f"{parts[0]}-{parts[1]}")
    # strip common suffixes
    for k in list(keys):
        if k.endswith('generic'):
            keys.add(k.replace('-generic', ''))
        if k.endswith('amd64'):
            keys.add(k.replace('-amd64', ''))
    return set(k for k in keys if k)

class KernelInfo:
    def __init__(self, version, status="mevcut değil", installed_date=None):
        self.version = version
        self.status = status
        self.installed_date = installed_date
        self.size = None
        self.url = None
        self.release_date = None
        self.build_date = None
        self.debs = []

    def is_installed(self):
        # basit kontrol: installed_map varsa ona bak
        try:
            inst_map = getattr(sys.modules[__name__], 'installed_map', None)
            if inst_map and self.version in inst_map:
                return True
            # fallback: /boot içeriklerini kontrol et
            if os.path.exists('/boot'):
                for fn in os.listdir('/boot'):
                    if self.version in fn:
                        return True
        except Exception:
            pass
        return False

    def is_newer_than_current(self):
        try:
            current = subprocess.check_output(['uname', '-r']).decode().strip()
            cur_core = current.split('-')[0]
            ver_core = self.version.split('-')[0]
            return normalize_version_str(ver_core) > normalize_version_str(cur_core)
        except Exception:
            return False

class KernelInstallWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, kernel, headers=None, download_path=None):
        super().__init__()
        self.kernel = kernel
        self.headers = headers or {'User-Agent': 'Mozilla/5.0'}
        self.download_path = download_path or tempfile.mkdtemp(prefix='kernel_install_')
        self._cancel = False

    def run(self):
        try:
            self.progress.emit(f"İşlem başlatılıyor: {self.kernel.version}")
            os.makedirs(self.download_path, exist_ok=True)

            page_url = self.kernel.url
            if not page_url:
                raise RuntimeError('Kernel indirme sayfası bulunamadı')

            self.progress.emit('Paket listesi alınıyor...')
            r = requests.get(page_url, headers=self.headers, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')

            # Paket linklerini kategorize et
            header_base_links = []  # linux-headers-6.x.x
            header_generic_links = []  # linux-headers-6.x.x-generic
            module_links = []  # linux-modules
            image_links = []  # linux-image
            
            for a in soup.find_all('a'):
                href = a.get('href', '')
                if not href.endswith('.deb'):
                    continue
                    
                # Linki normalize et
                if href.startswith('http'):
                    full_url = href
                else:
                    base = page_url.rstrip('/') + '/'
                    full_url = base + href

                # Sadece amd64 paketlerini al
                if not ('/amd64/' in full_url or '_amd64.deb' in full_url or 'amd64' in full_url):
                    continue

                # Paketleri kategorize et
                basename = os.path.basename(full_url)
                if 'headers' in basename:
                    if 'generic' in basename:
                        header_generic_links.append(full_url)
                    else:
                        header_base_links.append(full_url)
                elif 'modules' in basename:
                    module_links.append(full_url)
                elif 'image' in basename:
                    image_links.append(full_url)

            # Paketleri doğru sırayla birleştir
            download_links = header_base_links + header_generic_links + module_links + image_links

            if not download_links:
                raise RuntimeError('Gerekli kernel paketleri bulunamadı.')

            # İndirme
            self.progress.emit(f"{len(download_links)} paket sırayla indiriliyor...")
            downloaded = []
            for idx, link in enumerate(download_links, 1):
                if self._cancel:
                    raise RuntimeError('Kullanıcı tarafından iptal edildi')
                    
                fname = os.path.join(self.download_path, os.path.basename(link.split('?')[0]))
                self.progress.emit(f"İndiriliyor [{idx}/{len(download_links)}]: {os.path.basename(fname)}")
                
                with requests.get(link, headers=self.headers, stream=True, timeout=30) as resp:
                    resp.raise_for_status()
                    with open(fname, 'wb') as fd:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                fd.write(chunk)
                downloaded.append(fname)

            if not downloaded:
                raise RuntimeError('İndirilen paket yok')

            # Kurulum scripti hazırla
            self.progress.emit('Paketler kuruluyor...')
            script_lines = [
                "#!/bin/sh",
                "set -e",
                "export DEBIAN_FRONTEND=noninteractive",
                # Önce bağımlılıkları çözmeye çalış
                "apt-get update -y || true",
                "apt-get install -y libc6 libelf1 libssl3 || true",
                # Paketleri sırayla ve zorla kur
                "for deb in " + " ".join([f"'{d}'" for d in downloaded]) + "; do",
                "    dpkg --force-depends -i \"$deb\" || true",
                "done",
                # Eksik bağımlılıkları otomatik kur
                "apt-get install -f -y",
                # GRUB güncelle
                "update-grub || true"
            ]

            script_path = os.path.join(self.download_path, 'install_kernel.sh')
            with open(script_path, 'w') as sf:
                sf.write('\n'.join(script_lines))
            os.chmod(script_path, 0o755)

            # escalate ile çalıştır
            cmd = escalate_cmd([script_path])
            self.progress.emit('Yönetici yetkisi penceresi açılabilir. Lütfen onaylayın.')
            # stdout/stderr'i dosyalara yönlendirip arka planda okuyacağız; bu polkit/pkexec prompt'unu engellemez
            out_file = os.path.join(self.download_path, 'install_out.log')
            err_file = os.path.join(self.download_path, 'install_err.log')
            with open(out_file, 'w+') as outf, open(err_file, 'w+') as errf:
                try:
                    proc = subprocess.Popen(cmd, stdout=outf, stderr=errf)
                except Exception as e:
                    raise RuntimeError(f'Yönetici komutu başlatılamadı: {str(e)}')

                # Süreç çalışırken log dosyalarını takip et
                last_out_pos = 0
                last_err_pos = 0
                while True:
                    if proc.poll() is not None:
                        # süreç bitmiş; son içerikleri oku
                        outf.flush(); errf.flush()
                        with open(out_file, 'r') as rf:
                            rf.seek(last_out_pos)
                            data = rf.read()
                            if data:
                                self.progress.emit(data)
                        with open(err_file, 'r') as rf:
                            rf.seek(last_err_pos)
                            data = rf.read()
                            if data:
                                self.progress.emit(data)
                        break

                    # ara ara log dosyalarını oku
                    with open(out_file, 'r') as rf:
                        rf.seek(last_out_pos)
                        data = rf.read()
                        if data:
                            last_out_pos = rf.tell()
                            # gönder
                            for line in data.splitlines():
                                self.progress.emit(line)
                    with open(err_file, 'r') as rf:
                        rf.seek(last_err_pos)
                        data = rf.read()
                        if data:
                            last_err_pos = rf.tell()
                            for line in data.splitlines():
                                self.progress.emit(line)

                    # kısa bekleme
                    time.sleep(0.5)

                # Süreç tamamlandıktan sonra dönüş kodunu kontrol et
                returncode = proc.returncode
                if returncode != 0:
                    # son stdout/stderr oku
                    with open(out_file, 'r') as rf:
                        stdout = rf.read()
                    with open(err_file, 'r') as rf:
                        stderr = rf.read()
                    raise RuntimeError(f'Kurulum komutu hata ile tamamlandı (code {returncode}). stdout:\n{stdout}\nstderr:\n{stderr}')

            # Temizlik
            try:
                os.remove(script_path)
            except Exception:
                pass

            self.progress.emit('Kurulum tamamlandı, temizlik yapılıyor...')
            try:
                shutil.rmtree(self.download_path)
            except Exception:
                pass

            self.finished.emit(True, 'Kernel başarıyla kuruldu. Sisteminizi Yeniden Başlatınız Grub Ekranında Advanced options menüsünden Kurduğunuz KERNEL Sürümünü Seçebilirsiniz.')

        except Exception as e:
            # Temizlik
            try:
                shutil.rmtree(self.download_path)
            except Exception:
                pass
            self.finished.emit(False, f'Hata: {str(e)}')

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SELIKUP - Secure Linux Kernel Updater")
        self.setMinimumSize(1000, 700)
        # Pencere ikonunu ayarla
        logo_path = get_logo_path()
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        self.available_kernels = []
        self.installed_kernels = []
        self.current_kernel_version = None
        self.setup_ui()
        # Önce yüklü kernelleri kontrol et
        self.load_installed_kernels()
        # Sonra mevcut kernel listesini yükle
        self.load_available_kernels()

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Başlık
        title = QLabel("SELIKUP - Secure Linux Kernel Updater")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #E0E0E0; margin: 20px;")
        layout.addWidget(title)

        # Tab widget
        self.tab_widget = QTabWidget()
        kernel_tab = QWidget()
        kernel_layout = QVBoxLayout(kernel_tab)

        # Kernel listesi
        self.kernel_tree = QTreeWidget()
        self.kernel_tree.setHeaderLabels(["Çekirdek", "Durum", "Tarih"])
        self.kernel_tree.setStyleSheet("""
            QTreeWidget {
                background-color: #1E1E1E;
                color: #E0E0E0;
                border: 1px solid #555555;
                border-radius: 3px;
            }
            QTreeWidget::item {
                padding: 5px;
            }
            QTreeWidget::item:selected {
                background-color: #1976D2;
            }
        """)
        kernel_layout.addWidget(self.kernel_tree)

        # Butonlar
        button_layout = QHBoxLayout()
        
        self.refresh_button = QPushButton("Yenile")
        self.refresh_button.clicked.connect(self.load_available_kernels)
        self.refresh_button.setStyleSheet(self.get_button_style("#424242"))
        
        self.install_button = QPushButton("Kur")
        self.install_button.clicked.connect(self.install_kernel)
        self.install_button.setStyleSheet(self.get_button_style("#2196F3"))
        
        self.uninstall_button = QPushButton("Kaldır")
        self.uninstall_button.clicked.connect(self.uninstall_kernel)
        self.uninstall_button.setStyleSheet(self.get_button_style("#F44336"))

        # --- YENİ: Aktif Et butonu ---
        self.set_active_button = QPushButton("Aktif Et")
        self.set_active_button.clicked.connect(self.set_kernel_active)
        self.set_active_button.setStyleSheet(self.get_button_style("#00C853"))
        self.set_active_button.setVisible(False)  # --- Butonu gizle ---

        button_layout.addWidget(self.refresh_button)
        button_layout.addWidget(self.install_button)
        button_layout.addWidget(self.uninstall_button)
        button_layout.addWidget(self.set_active_button)
        kernel_layout.addLayout(button_layout)

        # Log sekmesi
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E;
                color: #E0E0E0;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 10px;
                font-family: monospace;
            }
        """)
        log_layout.addWidget(self.log_area)

        # Hakkında sekmesi
        about_tab = QWidget()
        about_layout = QVBoxLayout(about_tab)
        about_layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        # Logo ekle
        logo_path = get_logo_path()
        if os.path.exists(logo_path):
            logo_label = QLabel()
            pixmap = QPixmap(logo_path)
            logo_label.setPixmap(pixmap.scaledToWidth(180, Qt.SmoothTransformation))
            logo_label.setAlignment(Qt.AlignCenter)
            about_layout.addWidget(logo_label)

        # Program bilgisi
        info_label = QLabel(
            "<b>SELIKUP - Secure Linux Kernel Updater</b><br>"
            "Linux çekirdeğini kolayca güncelleyin ve yönetin.<br><br>"
            "<b>Sürüm:</b> 1.0.0<br>"
            "<b>Yazılımcı:</b> Fatih ÖNDER (CekToR)<br>"
            "<b>E-posta:</b> fatih@algyazilim.com<br>"
            "<b>Web:</b> https://algyazilim.com<br>"
            "<b>Web:</b> https://fatihonder.org.tr<br>"
            "<b>Github:</b> <a href='https://github.com/cektor'>CekToR</a><br><br>"
            "<b>Lisans:</b>GNU/GPL<br>"
            "Bu program, KERNEL ana çekirdek deposundan kernel indirip kurmanıza yardımcı olur.<br>"
            "Kullanım tamamen kendi sorumluluğunuzdadır. Uyumsuzluk riskleri bulunduğunu UNUTMAYIN!. İşlem öncesi yedek alınması tavsiye edilir.<br>"
            "Bu Yazılım Hiçbir Garanti Vermemektedir.<br>"
        )
        info_label.setOpenExternalLinks(True)
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setStyleSheet("font-size: 15px; color: #E0E0E0; margin: 10px;")
        about_layout.addWidget(info_label)

        # Sekmeleri ekle
        self.tab_widget.addTab(kernel_tab, "Kernel Listesi")
        self.tab_widget.addTab(log_tab, "Günlük")
        self.tab_widget.addTab(about_tab, "Hakkında")
        layout.addWidget(self.tab_widget)

        # Durum çubuğu
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet("""
            QStatusBar {
                background-color: #1A1A1A;
                color: #E0E0E0;
                border-top: 1px solid #555555;
            }
        """)
        self.setStatusBar(self.status_bar)

        # İlerleme çubuğu
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #555555;
                border-radius: 3px;
                text-align: center;
                background-color: #2D2D2D;
                max-width: 200px;
            }
            QProgressBar::chunk {
                background-color: #2196F3;
            }
        """)
        self.progress_bar.hide()
        self.status_bar.addPermanentWidget(self.progress_bar)

        # Koyu tema
        self.setup_dark_theme()

    def load_available_kernels(self):
        self.status_bar.showMessage("Mevcut kernel sürümleri yükleniyor...")
        self.log_area.append("[*] Kernel sürümleri kontrol ediliyor...")
        self.progress_bar.show()
        self.progress_bar.setMaximum(0)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        try:
            base_url = "https://kernel.ubuntu.com/~kernel-ppa/mainline"
            response = requests.get(f"{base_url}/", headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            kernels = []
            
            for link in soup.find_all('a'):
                href = link.get('href', '')
                if href.startswith('v'):
                    version = href.strip('v').strip('/')
                    kernel = KernelInfo(version)
                    kernel.url = f"{base_url}/{href}"
                    
                    # Sürüm kategorisini belirle
                    try:
                        # Sürüm numarasını temizle ve ayrıştır
                        clean_version = version.split('-')[0]  # Ubuntu sürüm eklerini kaldır
                        version_parts = clean_version.split('.')
                        
                        # Sadece sayısal sürüm numaralarını işle
                        if version_parts and version_parts[0].isdigit():
                            major = int(version_parts[0])
                            minor = 0
                            if len(version_parts) > 1 and version_parts[1].split('-')[0].isdigit():
                                minor = int(version_parts[1].split('-')[0])
                            
                            if 'rc' in version.lower():
                                if major >= 6:  # Sadece güncel RC sürümlerini göster
                                    kernel.status = "RC (Test Sürümü)"
                                else:
                                    continue  # Eski RC sürümlerini atla
                            else:
                                if major > 6 or (major == 6 and minor >= 5):
                                    kernel.status = "En Son Sürüm"
                                elif major == 6 and minor >= 1:
                                    kernel.status = "Kararlı Sürüm"
                                else:
                                    kernel.status = "Eski Sürüm"
                            
                            kernels.append(kernel)
                    except (ValueError, IndexError) as e:
                        self.log_area.append(f"[!] Sürüm ayrıştırma hatası ({version}): {str(e)}")
                        continue  # Hatalı sürümleri atla
                        
                        kernels.append(kernel)
            
            self.available_kernels = sorted(kernels, key=lambda k: k.version, reverse=True)
            self.update_kernel_tree()
            
            self.progress_bar.hide()
            self.log_area.append(f"[+] {len(kernels)} kernel sürümü bulundu.")
            self.status_bar.showMessage("Kernel listesi güncellendi.", 5000)
            
        except Exception as e:
            self.progress_bar.hide()
            self.log_area.append(f"[-] Hata: {str(e)}")
            self.status_bar.showMessage("Kernel sürümleri yüklenemedi!", 5000)
            self.show_error_in_tree("Kernel sürümleri yüklenemedi!")

    def update_kernel_tree(self):
        """Kernelleri mantıklı hiyerarşik sırada ve kategorilerde gösterir.
        - En Son Sürümler (major>6 veya 6.x yeni)
        - Kararlı Sürümler (güncel stabil series)
        - Test Sürümleri (RC)
        - Eski Sürümler (varsayılan kapalı)
        """
        self.kernel_tree.clear()
        self.kernel_tree.setColumnCount(3)
        self.kernel_tree.setHeaderLabels(["Çekirdek", "Durum", "Tarih"])

        latest_category = QTreeWidgetItem(["En Son Sürümler"])
        stable_category = QTreeWidgetItem(["Kararlı Sürümler"])
        rc_category = QTreeWidgetItem(["Test Sürümleri (RC)"])
        old_category = QTreeWidgetItem(["Eski Sürümler"])

        latest_category.setBackground(0, QColor("#1A237E"))
        stable_category.setBackground(0, QColor("#1B5E20"))
        rc_category.setBackground(0, QColor("#BF360C"))
        old_category.setBackground(0, QColor("#424242"))

        self.kernel_tree.addTopLevelItem(latest_category)
        self.kernel_tree.addTopLevelItem(stable_category)
        self.kernel_tree.addTopLevelItem(rc_category)
        self.kernel_tree.addTopLevelItem(old_category)

        # Grupları doldur
        groups = {'latest': [], 'stable': [], 'rc': [], 'old': []}
        for k in self.available_kernels:
            ver_tuple = normalize_version_str(k.version)
            if 'rc' in k.version.lower():
                groups['rc'].append(k)
            elif ver_tuple[0] > 6 or (ver_tuple[0] == 6 and ver_tuple[1] >= 5):
                groups['latest'].append(k)
            elif ver_tuple[0] == 6:
                groups['stable'].append(k)
            else:
                groups['old'].append(k)

        # Numerik olarak sıralama (descending)
        for key in groups:
            groups[key].sort(key=lambda kk: normalize_version_str(kk.version), reverse=True)

        def make_item(kernel):
            # installed_map üzerinden bir eşleşme ara
            inst = None
            installed_map = getattr(self, 'installed_map', {})
            # check variants of kernel.version
            for key in version_key_variants(kernel.version):
                if key in installed_map:
                    inst = installed_map[key]
                    break
            # ayrıca çekirdeğin core sürümü ile eşleştir (örn 6.16.4)
            if not inst:
                core_key = kernel.version.split('-')[0]
                if core_key in installed_map:
                    inst = installed_map[core_key]

            if inst:
                ks = inst.status
                if ks == 'Aktif Kernel':
                    item = QTreeWidgetItem([f"{kernel.version} (Aktif)", ks, kernel.build_date or ""]) 
                    item.setForeground(0, QColor("#00C853"))
                    item.setForeground(1, QColor("#00C853"))
                    item.setIcon(0, QIcon.fromTheme("emblem-default"))
                    return item
                else:
                    item = QTreeWidgetItem([kernel.version, ks, kernel.build_date or ""]) 
                    item.setForeground(1, QColor("#4CAF50"))
                    item.setIcon(0, QIcon.fromTheme("dialog-ok"))
                    return item
            else:
                item = QTreeWidgetItem([kernel.version, "Sisteminde Mevcut değil", kernel.build_date or ""]) 
                item.setIcon(0, QIcon.fromTheme("package"))
                return item

        # Ekleme sırası: latest, stable, rc, old
        for k in groups['latest']:
            latest_category.addChild(make_item(k))
        for k in groups['stable']:
            stable_category.addChild(make_item(k))
        for k in groups['rc']:
            rc_category.addChild(make_item(k))
        for k in groups['old']:
            old_category.addChild(make_item(k))

        # Expand ve görünüm ayarları
        latest_category.setExpanded(True)
        stable_category.setExpanded(True)
        rc_category.setExpanded(False)
        old_category.setExpanded(False)

        self.kernel_tree.resizeColumnToContents(0)
        self.kernel_tree.resizeColumnToContents(1)
        self.kernel_tree.resizeColumnToContents(2)

    def show_error_in_tree(self, message):
        self.kernel_tree.clear()
        error_item = QTreeWidgetItem([message])
        error_item.setForeground(0, QColor("red"))
        self.kernel_tree.addTopLevelItem(error_item)

    def setup_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1A1A1A;
            }
            QWidget {
                background-color: #1A1A1A;
                color: #E0E0E0;
            }
        """)

    def get_button_style(self, color):
        return f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background-color: {color}DD;
            }}
            QPushButton:pressed {{
                background-color: {color}AA;
            }}
            QPushButton:disabled {{
                background-color: #666666;
            }}
        """

    def load_installed_kernels(self):
        try:
            self.installed_kernels = []
            self.installed_map = {}

            # Aktif kernel
            try:
                current_kernel = subprocess.check_output(['uname', '-r']).decode().strip()
                self.current_kernel_version = current_kernel
            except Exception:
                self.current_kernel_version = None

            found_versions = set()

            # 1) /boot içinden vmlinuz-* dosyalarını tarayarak versiyonları al
            try:
                if os.path.isdir('/boot'):
                    for fn in os.listdir('/boot'):
                        if fn.startswith('vmlinuz-'):
                            ver = fn[len('vmlinuz-'):]
                            found_versions.add(ver)
            except Exception:
                pass

            # 2) dpkg ile kurulu linux-image paketlerini oku (daha kesin)
            try:
                out = subprocess.check_output(
                    ['dpkg-query', '-W', '-f=${Package}\t${Status}\t${Version}\n', 'linux-image*', 'linux-headers*', 'linux-modules*'],
                    stderr=subprocess.DEVNULL
                ).decode(errors='ignore')
            except subprocess.CalledProcessError:
                out = ''
            except FileNotFoundError:
                out = ''

            dpkg_versions = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                pkg, status, ver = parts[0], parts[1], parts[2]
                # Sadece kurulu olanları ekle
                if status == "install" or status == "installed":
                    if pkg.startswith('linux-image-'):
                        dpkg_versions.add(pkg.replace('linux-image-', ''))
                    elif pkg.startswith('linux-headers-'):
                        dpkg_versions.add(pkg.replace('linux-headers-', ''))
                    elif pkg.startswith('linux-modules-'):
                        dpkg_versions.add(pkg.replace('linux-modules-', ''))

            # 3) Ayrıca dpkg -l çıktısından headers/moduler vb. paketleri parse et (fallback)
            try:
                out2 = subprocess.check_output(['dpkg', '-l'], stderr=subprocess.DEVNULL).decode(errors='ignore')
                for line in out2.splitlines():
                    if line.startswith('ii '):
                        cols = line.split()
                        if len(cols) >= 2:
                            pkg = cols[1]
                            if pkg.startswith('linux-image-'):
                                dpkg_versions.add(pkg.replace('linux-image-', ''))
                            elif pkg.startswith('linux-headers-'):
                                dpkg_versions.add(pkg.replace('linux-headers-', ''))
                            elif pkg.startswith('linux-modules-'):
                                dpkg_versions.add(pkg.replace('linux-modules-', ''))
            except Exception:
                pass

            # Sadece hem dpkg'de kurulu olan hem de /boot'ta dosyası olanları birleştir
            # Ama asıl güvenilir kaynak dpkg'de kurulu olanlardır
            found_versions = dpkg_versions

            # Aktif kernel her zaman eklenmeli
            if self.current_kernel_version:
                found_versions.add(self.current_kernel_version)

            # Oluşan versiyon setinden KernelInfo objeleri oluştur
            for ver in sorted(found_versions):
                status = 'Kurulu'
                if self.current_kernel_version and (ver == self.current_kernel_version or ver in self.current_kernel_version or self.current_kernel_version.startswith(ver.split('-')[0])):
                    status = 'Aktif Kernel'
                k = KernelInfo(ver, status)
                self.installed_kernels.append(k)
                for key in version_key_variants(ver):
                    if key not in self.installed_map:
                        self.installed_map[key] = k

            # Ayrıca aktif kernel için şunu garanti et: active kernel tam hali ile map'e eklenmiş olsun
            if self.current_kernel_version:
                for key in version_key_variants(self.current_kernel_version):
                    if key not in self.installed_map:
                        k = KernelInfo(self.current_kernel_version, 'Aktif Kernel')
                        self.installed_kernels.append(k)
                        for kk in version_key_variants(self.current_kernel_version):
                            self.installed_map[kk] = k

            if self.current_kernel_version:
                short = self.current_kernel_version.split('-')[0]
                self.log_area.append(f"[*] Aktif kernel sürümü: {self.current_kernel_version} (kısa: {short})")
        except Exception as e:
            self.log_area.append(f"[-] Hata: Yüklü kerneller listelenemedi - {str(e)}")
            self.current_kernel_version = None

    def install_kernel(self):
        # Seçili öğeyi al
        selected_items = self.kernel_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, 'Uyarı', 'Lütfen bir kernel sürümü seçin!')
            return
        item = selected_items[0]
        # Eğer seçilen bir kategori başlığıysa uyar
        if item.childCount() > 0 or item.text(1) == '':
            QMessageBox.warning(self, 'Uyarı', 'Lütfen bir kernel sürümü seçin (kategori değil).')
            return

        version_text = item.text(0).replace(' (Aktif)', '').strip()
        version = version_text
        kernel = next((k for k in self.available_kernels if k.version == version), None)
        if not kernel:
            QMessageBox.critical(self, 'Hata', 'Seçilen kernel bilgisi bulunamadı.')
            return

        # Eğer zaten kuruluysa uyar
        if getattr(self, 'installed_map', {}) and kernel.version in self.installed_map:
            QMessageBox.information(self, 'Bilgi', 'Seçilen kernel sistemde zaten yüklü.')
            return

        reply = QMessageBox.question(self, 'Kernel Kurulumu', f"Genelde kullandığınız dağıtımın varsayılan kernel sürümü daha kararlıdır. Kernel kurulumunun bağlılık uyumsuzluğuna neden olabileceğini unutmayın, sorumluluk kullanıcıya aittir.! {kernel.version} sürümünü kurmak istediğinizden emin misiniz?", QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Başlat
        self.log_area.append(f'[*] {kernel.version} kurulumu başlatılıyor...')
        self.progress_bar.show()
        self.progress_bar.setMaximum(0)

        # Kurulum başlarken Günlük sekmesine geç
        self.tab_widget.setCurrentIndex(1)  # 0: Kernel Listesi, 1: Günlük, 2: Hakkında

        self.install_worker = KernelInstallWorker(kernel, headers={'User-Agent':'Mozilla/5.0'}, download_path=None)
        self.install_worker.progress.connect(self.update_progress)
        self.install_worker.finished.connect(self.installation_finished)
        self.install_worker.start()

    def update_progress(self, message):
        self.log_area.append(message)
        self.status_bar.showMessage(message)

    def installation_finished(self, success, message):
        self.progress_bar.hide()
        self.log_area.append(message)
        if success:
            QMessageBox.information(self, 'Başarılı', message)
            # Yüklü kernel listesini güncelle
            self.load_installed_kernels()
            self.update_kernel_tree()
            # --- YENİ: Kurulan kernel'i varsayılan yap ---
            last_installed = None
            if hasattr(self, 'install_worker') and hasattr(self.install_worker, 'kernel'):
                last_installed = self.install_worker.kernel.version
            if last_installed:
                try:
                    set_kernel_as_default_grub(last_installed, self.log_area)
                    self.log_area.append(f"[+] {last_installed} sürümü otomatik olarak varsayılan kernel yapıldı.")
                except Exception as e:
                    self.log_area.append(f"[-] Varsayılan kernel ayarlanamadı: {e}")
        else:
            # Daha detaylı hata göster ve kurtarma seçenekleri sun
            # Eğer bağımlılık hatası ise kullanıcıya açıklama göster
            if "Bağımlılık kontrolü başarısız" in message:
                message += (
                    "\n\nÇözüm için:\n"
                    "- Tüm kernel .deb dosyalarını (headers, modules, image) eksiksiz indirdiğinizden emin olun.\n"
                    "- Sisteminizde libc6, libelf1t64, libssl3t64 gibi kütüphanelerin yeterli sürümde olduğundan emin olun.\n"
                    "- Debian veya eski Ubuntu kullanıyorsanız, Ubuntu ana çekirdekleri uyumsuz olabilir.\n"
                    "- Sistem güncellemesi veya daha güncel bir dağıtım kullanmanız gerekebilir.\n"
                    "- Kernel kurulumu için en güvenli yol, kendi dağıtımınızın kernel paketlerini kullanmaktır."
                )
            dlg = QMessageBox(self)
            dlg.setIcon(QMessageBox.Critical)
            dlg.setWindowTitle('Hata')
            dlg.setText('Kernel kurulumu başarısız oldu.')
            dlg.setDetailedText(message)
            dlg.setStandardButtons(QMessageBox.Ok)

            # Eğer hata dpkg/apt ile alakalıysa hızlı kurtarma düğmeleri ekle
            recovery = False
            if 'Bağımlılık kontrolü başarısız' in message or 'Kurulum komutu hata ile tamamlandı' in message or 'dpkg' in message or 'libc6' in message:
                recovery = True

            if recovery:
                rb = QMessageBox.question(self, 'Kurtarma', 'Sistem paketleri onarılmak isteniyor mu? (apt-get -f install ve dpkg --configure -a çalıştırılacaktır)', QMessageBox.Yes | QMessageBox.No)
                if rb == QMessageBox.Yes:
                    # çalıştır
                    tmpd = tempfile.mkdtemp(prefix='kernel_recover_')
                    script_path = os.path.join(tmpd, 'recover.sh')
                    with open(script_path, 'w') as sf:
                        sf.write('#!/bin/sh\nset -e\nexport DEBIAN_FRONTEND=noninteractive\n/usr/bin/dpkg --configure -a || true\n/usr/bin/apt-get update || true\n/usr/bin/apt-get install -f -y || true\n')
                    os.chmod(script_path, 0o755)
                    cmd = escalate_cmd([script_path])
                    try:
                        p = subprocess.Popen(cmd)
                        self.log_area.append('[*] Kurtarma işlemi başlatıldı. Tamamlanmasını bekleyin...')
                    except Exception as e:
                        self.log_area.append(f'[-] Kurtarma işlemi başlatılamadı: {e}')

            dlg.exec_()

    def uninstall_kernel(self):
        selected_items = self.kernel_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Uyarı", "Lütfen bir kernel sürümü seçin!")
            return

        version = selected_items[0].text(0).replace(' (Aktif)', '').strip()
        current_kernel = subprocess.check_output(['uname', '-r']).decode().strip()

        # Aktif kernel ise kaldırma!
        if version == current_kernel or version in current_kernel:
            QMessageBox.warning(self, "Uyarı", "Aktif kullanılan kernel kaldırılamaz!")
            return

        # installed_map'te varyantlarla kontrol et
        is_installed = False
        if hasattr(self, 'installed_map'):
            for key in version_key_variants(version):
                if key in self.installed_map:
                    is_installed = True
                    break

        if not is_installed:
            QMessageBox.warning(self, "Uyarı", "Seçilen kernel sistemde kurulu değil!")
            return

        reply = QMessageBox.question(
            self,
            "Kernel Kaldırma",
            f"{version} sürümünü sistemden tamamen kaldırmak istediğinizden emin misiniz?\n\nBu işlem headers, image, modules paketlerini ve /boot altındaki dosyaları siler.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        self.log_area.append(f"[*] {version} kaldırılıyor...")

        # Ubuntu Mainline Kernel Installer'daki gibi apt-get remove --purge ile kaldır
        try:
            tmpd = tempfile.mkdtemp(prefix='kernel_remove_')
            script_path = os.path.join(tmpd, 'remove_kernel.sh')
            # Paket adlarını oluştur
            pkg_patterns = [
                f"linux-image-{version}",
                f"linux-image-unsigned-{version}",
                f"linux-headers-{version}",
                f"linux-modules-{version}",
                f"linux-image-extra-{version}",
                f"linux-modules-extra-{version}",
            ]
            # /boot altındaki dosyaları da sil
            boot_patterns = [
                f"/boot/vmlinuz-{version}",
                f"/boot/initrd.img-{version}",
                f"/boot/System.map-{version}",
                f"/boot/config-{version}",
            ]
            script_lines = [
                "#!/bin/sh",
                "set -e",
                "export DEBIAN_FRONTEND=noninteractive",
                "echo 'Preparing to uninstall selected kernels'",
                "for pkg in " + " ".join([f"'{p}'" for p in pkg_patterns]) + "; do",
                "    if dpkg -l | grep -q \"^ii  $pkg \"; then",
                "        echo \"Kaldırılıyor: $pkg ...\"",
                "        apt-get remove --purge -y $pkg || true",
                "    fi",
                "done",
                "for f in " + " ".join([f"'{b}'" for b in boot_patterns]) + "; do",
                "    [ -e \"$f\" ] && rm -f \"$f\" || true",
                "done",
                "update-initramfs -u -k all || true",
                "update-grub || true",
                "echo 'Un-install completed'"
            ]
            with open(script_path, 'w') as sf:
                sf.write('\n'.join(script_lines))
            os.chmod(script_path, 0o755)
            cmd = escalate_cmd([script_path])
            # Çıktıyı GUI log'a aktar
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                self.log_area.append(line.rstrip())
                QApplication.processEvents()
            proc.wait()
            if proc.returncode == 0:
                self.log_area.append(f"[+] {version} başarıyla kaldırıldı.")
                QMessageBox.information(self, "Başarılı", f"{version} başarıyla kaldırıldı.")
            else:
                self.log_area.append(f"[-] Kaldırma sırasında hata oluştu (kod {proc.returncode})")
                QMessageBox.critical(self, "Hata", f"Kaldırma sırasında hata oluştu (kod {proc.returncode})")
            try:
                shutil.rmtree(tmpd)
            except Exception:
                pass
            self.load_installed_kernels()
            self.update_kernel_tree()
        except Exception as e:
            self.log_area.append(f"[-] Kernel kaldırılırken hata oluştu: {e}")
            QMessageBox.critical(self, "Hata", f"Kernel kaldırılırken hata oluştu: {e}")

    def set_kernel_active(self):
        """Seçili kurulu kernel'i GRUB'da varsayılan yapar."""
        selected_items = self.kernel_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Uyarı", "Lütfen bir kernel sürümü seçin!")
            return

        item = selected_items[0]
        version = item.text(0).replace(' (Aktif)', '').strip()
        # Sadece kurulu kernel'ler için izin ver
        if getattr(self, 'installed_map', {}) and version not in self.installed_map:
            QMessageBox.warning(self, "Uyarı", "Seçilen kernel sistemde kurulu değil!")
            return

        # Aktif kernel zaten bu ise uyar
        current_kernel = subprocess.check_output(['uname', '-r']).decode().strip()
        if version == current_kernel or version in current_kernel:
            QMessageBox.information(self, "Bilgi", "Seçilen kernel zaten aktif olarak kullanılıyor.")
            return

        reply = QMessageBox.question(
            self,
            "Kernel'i Aktif Et",
            f"{version} sürümünü bir sonraki açılışta varsayılan yapmak istiyor musunuz?\n\nBu işlem GRUB önyükleyici ayarlarını güncelleyecek ve sistem yeniden başlatıldığında bu kernel ile açılacaktır.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # GRUB'da kernel'i varsayılan yap
        self.log_area.append(f"[*] {version} sürümü GRUB'da varsayılan yapılıyor...")
        self.progress_bar.show()
        self.progress_bar.setMaximum(0)
        QApplication.processEvents()
        try:
            set_kernel_as_default_grub(version, self.log_area)
            self.progress_bar.hide()
            QMessageBox.information(self, "Başarılı", f"{version} sürümü bir sonraki açılışta varsayılan kernel olarak ayarlandı.\n\nDeğişikliğin etkin olması için sistemi yeniden başlatmalısınız.")
            self.log_area.append(f"[+] {version} sürümü başarıyla varsayılan kernel olarak ayarlandı.")
        except Exception as e:
            self.progress_bar.hide()
            QMessageBox.critical(self, "Hata", f"GRUB ayarlanırken hata oluştu: {e}")
            self.log_area.append(f"[-] GRUB ayarlanırken hata oluştu: {e}")

# --- set_kernel_as_default_grub fonksiyonunu en üste taşı! ---
def set_kernel_as_default_grub(kernel_version, log_area=None):
    """
    Verilen kernel_version'ı GRUB'da varsayılan olarak ayarlar ve update-grub çalıştırır.
    Tüm dosya yazma ve update işlemleri yükseltilmiş (root) izinle yapılır.
    """
    import tempfile, shutil

    grub_cfg = "/boot/grub/grub.cfg"
    grub_default = "/etc/default/grub"

    # --- GRUB menü index'ini yükseltilmiş izinle oku ---
    try:
        tmp_grub_cfg = tempfile.NamedTemporaryFile("w", delete=False)
        tmp_grub_cfg.close()
        cmd_cp_grub = escalate_cmd(["cp", grub_cfg, tmp_grub_cfg.name])
        subprocess.check_call(cmd_cp_grub)
        menuentries = []
        with open(tmp_grub_cfg.name, "r") as f:
            for line in f:
                if line.strip().startswith("menuentry "):
                    menuentries.append(line.strip())
        os.unlink(tmp_grub_cfg.name)
    except Exception as e:
        raise RuntimeError(f"GRUB menü girdileri okunamadı: {e}")

    def kernel_in_entry(entry, version):
        patterns = [
            version,
            version.replace('.', '\\.'),
            f"vmlinuz-{version}",
            f"Linux {version}",
            f"linux-{version}",
            f"{version}-generic",
            f"{version}-amd64",
            f"{version} ",
            f"{version})",
            f"{version}\\)",
            f"{version}-",
        ]
        for pat in patterns:
            if pat in entry:
                return True
        if version.lower() in entry.lower():
            return True
        return False

    target_index = None
    for idx, entry in enumerate(menuentries):
        if kernel_in_entry(entry, kernel_version):
            target_index = idx
            break

    if target_index is None:
        pardus_idx = None
        debian_idx = None
        linux_idx = None
        for idx, entry in enumerate(menuentries):
            if "Pardus GNU/Linux" in entry:
                pardus_idx = idx
            elif "Debian GNU/Linux" in entry:
                debian_idx = idx
            elif "Linux" in entry:
                linux_idx = idx
        if pardus_idx is not None:
            target_index = pardus_idx
            if log_area:
                log_area.append("[!] İstenen çekirdek menü girişi bulunamadı, Pardus ana menüsü varsayılan yapılıyor.")
        elif debian_idx is not None:
            target_index = debian_idx
            if log_area:
                log_area.append("[!] İstenen çekirdek menü girişi bulunamadı, Debian ana menüsü varsayılan yapılıyor.")
        elif linux_idx is not None:
            target_index = linux_idx
            if log_area:
                log_area.append("[!] İstenen çekirdek menü girişi bulunamadı, ilk Linux menüsü varsayılan yapılıyor.")
        else:
            raise RuntimeError(
                f"{kernel_version} sürümüne ait GRUB menü girişi bulunamadı!\n"
                f"GRUB menü girişleri şunlardır (ilk 10):\n" +
                "\n".join(menuentries[:10])
            )

    if not os.path.exists(grub_default):
        raise RuntimeError("/etc/default/grub dosyası bulunamadı!")

    lines = []
    found = False
    with open(grub_default, "r") as f:
        for line in f:
            if line.strip().startswith("GRUB_DEFAULT="):
                lines.append(f'GRUB_DEFAULT={target_index}\n')
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f'GRUB_DEFAULT={target_index}\n')

    tmpf = tempfile.NamedTemporaryFile("w", delete=False)
    tmpf.writelines(lines)
    tmpf.close()

    # --- Yükseltilmiş izinle dosya kopyalama ve update-grub ---
    try:
        cmd_cp = escalate_cmd(["cp", tmpf.name, grub_default])
        subprocess.check_call(cmd_cp)
        os.unlink(tmpf.name)
        # --- update-grub komutunu pkexec ile değil, sudo ile çalıştır! ---
        # pkexec ile update-grub çalışmaz, çünkü /usr/sbin update-grub çoğu sistemde sadece root PATH'te olur.
        # sudo ile çalıştırmak daha güvenli ve yaygın.
        if shutil.which("sudo"):
            cmd_update = ["sudo", "-E", "update-grub"]
        else:
            cmd_update = ["update-grub"]
        subprocess.check_call(cmd_update)
    except Exception as e:
        try:
            os.unlink(tmpf.name)
        except Exception:
            pass
        raise RuntimeError(f"GRUB ayarlanırken hata oluştu: {e}")

    if log_area:
        log_area.append(f"[+] GRUB_DEFAULT={target_index} olarak ayarlandı ve update-grub çalıştırıldı.")

def _extract_numeric_version(s):
    # Örn '2.36-0ubuntu3' -> '2.36'
    if not s:
        return ''
    s = str(s)
    # split by - or : and take first part that starts with digit
    parts = re.split('[-:~+]', s)
    for p in parts:
        if re.match(r'^\d', p):
            m = re.match(r'^(\d+(?:\.\d+){0,2})', p)
            if m:
                return m.group(1)
    # fallback: take leading digits
    m = re.match(r'^(\d+(?:\.\d+){0,2})', s)
    return m.group(1) if m else ''


def _compare_numeric_versions(a, b):
    # return True if a >= b
    if not a or not b:
        return False
    ta = normalize_version_str(_extract_numeric_version(a))
    tb = normalize_version_str(_extract_numeric_version(b))
    return ta >= tb


def check_deb_dependencies(deb_files):
    """Her bir .deb için dpkg-deb -f <deb> Depends ile bağımlılıkları alır ve sistemde kurulu paketlerle karşılaştırır.
    döndürür: (True, '') ya da (False, 'detaylı mesaj')
    Bu kontrol kaba ama birçok uyumsuzluğu yakalar (örneğin libc6 >= 2.38 gereksinimi).
    """
    problems = []
    for deb in deb_files:
        try:
            out = subprocess.check_output(['dpkg-deb', '-f', deb, 'Depends'], stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError:
            out = ''
        except FileNotFoundError:
            # dpkg-deb yoksa sistemde kontrol yapılamaz
            return True, ''
        if not out:
            continue
        # bağımlılık satırı virgülle ayrılmış olabilir
        deps = [d.strip() for d in out.split(',') if d.strip()]
        for dep in deps:
            # bazen | ile alternatifler var; alttan ilk alternatifi seç
            alt = dep.split('|')[0].strip()
            # parantez içindeki versiyon bilgisini al
            m = re.match(r"^([^\(\s]+)\s*(?:\(([^\)]+)\))?", alt)
            if not m:
                continue
            pkg = m.group(1).strip()
            verreq = m.group(2).strip() if m.group(2) else ''
            # verreq ör: '>= 2.38'
            min_ver = ''
            if verreq:
                mm = re.match(r"(?:>=|=|<=)\s*([0-9\.:-~+]+)", verreq)
                if mm:
                    min_ver = mm.group(1)
            # sorgula sistemde kurulu mu
            try:
                inst_ver = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', pkg], stderr=subprocess.DEVNULL).decode().strip()
            except subprocess.CalledProcessError:
                inst_ver = ''
            if not inst_ver:
                problems.append(f"{os.path.basename(deb)} -> eksik paket: {pkg} (gereksinim: {verreq or 'herhangi'})")
            elif min_ver:
                if not _compare_numeric_versions(inst_ver, min_ver):
                    problems.append(f"{os.path.basename(deb)} -> {pkg} sürümü yetersiz: kurulu {inst_ver}, gerekli {min_ver}")
    if problems:
        return False, '\n'.join(problems)
    return True, ''

def get_logo_path():
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "selikup001.png")
    elif os.path.exists("/usr/share/icons/hicolor/48x48/apps/selikup001.png"):
        return "/usr/share/icons/hicolor/48x48/apps/selikup001.png"
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selikup001.png")
    if os.path.exists(local_path):
        return local_path
    return "selikup001.png"

if __name__ == '__main__':
    # GUI için X/Wayland ortam değişkeni kontrolü
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        print('Ekran ortamı bulunamadı. Lütfen X11/Wayland oturumu içinde çalıştırın.')
        print('DISPLAY veya WAYLAND_DISPLAY ortam değişkeni yok.')
        sys.exit(1)

    app = QApplication(sys.argv)
    # Program ikonu ekle (hem app hem window için)
    logo_path = get_logo_path()
    if os.path.exists(logo_path):
        icon = QIcon(logo_path)
        app.setWindowIcon(icon)
    else:
        icon = QIcon()

    # --- Sistem tepsisi simgesi ekle (dock/altbar için) ---
    tray_icon = QSystemTrayIcon()
    tray_icon.setIcon(icon)
    tray_icon.setVisible(True)
    # (İsteğe bağlı: Sağ tık menüsü eklenebilir)
    # menu = QMenu()
    # tray_icon.setContextMenu(menu)

    # Uygulama genelinde koyu tema
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(26, 26, 26))
    palette.setColor(QPalette.WindowText, QColor(224, 224, 224))
    palette.setColor(QPalette.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase, QColor(48, 48, 48))
    palette.setColor(QPalette.ToolTipBase, QColor(224, 224, 224))
    palette.setColor(QPalette.ToolTipText, QColor(224, 224, 224))
    palette.setColor(QPalette.Text, QColor(224, 224, 224))
    palette.setColor(QPalette.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ButtonText, QColor(224, 224, 224))
    palette.setColor(QPalette.BrightText, QColor(255, 255, 255))
    palette.setColor(QPalette.Link, QColor(33, 150, 243))
    palette.setColor(QPalette.Highlight, QColor(33, 150, 243))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)
    
    window = MainWindow()
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()
    sys.exit(app.exec_())
    # 3. update-grub çalıştır
    cmd2 = escalate_cmd(["update-grub"])
    subprocess.check_call(cmd2)

    if log_area:
        log_area.append(f"[+] GRUB_DEFAULT={target_index} olarak ayarlandı ve update-grub çalıştırıldı.")
