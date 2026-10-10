"""Test komunikasi serial dua arah dengan Arduino/ESP32.

Script ini:
1. Menghubungkan ke port serial
2. Mengirim pesan dan menunggu respons
3. Memungkinkan pengiriman berulang kali

Gunakan untuk:
- Cek koneksi Arduino tanpa menjalankan main.py
- Test komunikasi sebelum/pada saat menjalankan vending machine
"""
import sys
import time
import serial
import serial.tools.list_ports


BAUD_RATE = 9600
READY_TIMEOUT = 8.0
RESPONSE_TIMEOUT = 5.0


def list_available_ports():
    """Daftar port serial yang tersedia."""
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("Tidak ada port serial yang terdeteksi.")
        return []
    print("Port serial yang tersedia:")
    for i, p in enumerate(ports, 1):
        print(f"  {i}. {p.device} - {p.description}")
    return [p.device for p in ports]


def test_serial_connection(port, baud_rate=BAUD_RATE):
    """Test koneksi serial: kirim READY dan baca respons."""
    print(f"\n=== Test Koneksi Serial ===")
    print(f"Port: {port}")
    print(f"Baud rate: {baud_rate}")

    try:
        conn = serial.Serial(
            port=port,
            baudrate=baud_rate,
            timeout=2,
            write_timeout=2,
        )
        print(f"Port terbuka: {conn.is_open}")
        print(f"Timeout baca: {conn.read_timeout}")
        print(f"Timeout tulis: {conn.write_timeout}")

        # Tunggu sesaat untuk Arduino reset
        print("Menunggu 3 detik untuk Arduino reset...")
        time.sleep(3)

        # Coba baca data apaaja yang awalnya ada
        if conn.in_waiting > 0:
            data = conn.readline().decode("utf-8", "replace").strip()
            print(f"Data awal dari Arduino: '{data}'")
        else:
            print("Tidak ada data awal dari Arduino")

        # Kirim perintah PING
        print("\n--- Mengirim perintah PING ---")
        conn.write(b"PING\n")
        conn.flush()
        print("PING sudah dikirim")

        # Baca respons
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        response = None

        while time.monotonic() < deadline:
            if conn.in_waiting > 0:
                line = conn.readline().decode("utf-8", "replace").strip()
                if line:
                    response = line
                    print(f"Respons diterima: '{response}'")
                    break
            time.sleep(0.1)

        if response is None:
            print(f"Timeout {RESPONSE_TIMEOUT}s: Tidak ada respons dari Arduino")

        # Coba baca READY message
        print("\n--- Menunggu READY (max 8 detik) ---")
        ready_deadline = time.monotonic() + READY_TIMEOUT
        ready_received = False

        while time.monotonic() < ready_deadline:
            if conn.in_waiting > 0:
                line = conn.readline().decode("utf-8", "replace").strip()
                if line == "READY":
                    print("READY diterima dari Arduino!")
                    ready_received = True
                    break
                else:
                    print(f"Data lain: '{line}'")
            time.sleep(0.1)

        if not ready_received:
            print(f"Timeout {READY_TIMEOUT}s: READY tidak diterima")

        # Loop kiriman pesan
        print("\n--- Loop Kiriman Pesan ---")
        print("Ketik pesan untuk dikirim ke Arduino (atau 'quit' untuk keluar):")

        while True:
            user_input = input("> ").strip()
            if user_input.lower() in ('quit', 'exit', 'q'):
                print("Keluar dari test...")
                break

            if not user_input:
                continue

            print(f"Mengirim: '{user_input}'")
            try:
                conn.write((user_input + "\n").encode("ascii"))
                conn.flush()
                print("Sudah dikirim, menunggu respons...")

                # Baca respons
                resp_deadline = time.monotonic() + RESPONSE_TIMEOUT
                response = None

                while time.monotonic() < resp_deadline:
                    if conn.in_waiting > 0:
                        line = conn.readline().decode("utf-8", "replace").strip()
                        if line:
                            response = line
                            print(f"Respons: '{response}'")
                            break
                    time.sleep(0.1)

                if response is None:
                    print(f"Timeout: Tidak ada respons")
            except Exception as e:
                print(f"Error saat kirim: {type(e).__name__}: {e}")
                break

        conn.close()
        print("Port tertutup")

    except serial.SerialException as e:
        print(f"Gagal membuka port {port}: {e}")
        print("Pastikan:")
        print("  1. Port tidak digunakan aplikasi lain (seperti Arduino IDE Serial Monitor)")
        print("  2. Driver Arduino/CH340 terinstall")
        print("  3. Port benar (cek dengan 'ls /dev/ttyUSB*')")
    except Exception as e:
        print(f"Error tidak terduga: {type(e).__name__}: {e}")


if __name__ == "__main__":
    # Cari port otomatis atau gunakan argumen
    if len(sys.argv) > 1:
        port = sys.argv[1]
    else:
        ports = list_available_ports()
        if not ports:
            sys.exit(1)
        # Gunakan port pertama jika ada
        port = ports[0]

    test_serial_connection(port)
