"""Simple line-based serial communication with the Arduino dispenser."""

import time

import serial


READY_TIMEOUT = 8.0
RESPONSE_TIMEOUT = 5.0
serial_connection = None


def list_ports():
    """Daftar port serial yang tersedia."""
    return [p.device for p in serial.tools.list_ports.comports()]


def connect(port, baud_rate=9600):
    """Connect to the controller and wait for its READY message."""
    global serial_connection

    disconnect()
    try:
        connection = serial.Serial(
            port=port,
            baudrate=baud_rate,
            timeout=0.2,
            write_timeout=2.0,
        )
    except (OSError, serial.SerialException) as exc:
        raise RuntimeError(f"Tidak dapat membuka port {port}: {exc}") from exc

    serial_connection = connection

    try:
        connection.reset_input_buffer()
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            raw_line = connection.readline()
            response = raw_line.decode("utf-8", "replace").strip()
            # CH340/CP2102 sometimes prefixes garbage bytes before READY
            # Cek apakah READY ada di dalam baris (bisa di akhir baris setelah byte sampah)
            if "READY" in response:
                # Ekstrak hanya bagian READY-nya
                idx = response.find("READY")
                response = response[idx:idx+5].strip()
                if response == "READY":
                    return
            elif response == "READY":
                return
        raise TimeoutError(
            f"Tidak menerima READY dari kontroler pada {port}. "
            "Pastikan Arduino sudah di-reset (tekan tombol Reset) lalu jalankan again."
        )
    except (OSError, serial.SerialException, TimeoutError):
        disconnect()
        raise


def dispense(slot):
    """Ask the Arduino to dispense one product and wait for its confirmation."""
    if serial_connection is None or not serial_connection.is_open:
        raise RuntimeError("Kontroler Arduino belum terhubung.")
    if not isinstance(slot, int) or slot < 1:
        raise ValueError("Nomor slot dispenser harus berupa bilangan positif.")

    try:
        serial_connection.write(f"DISPENSE,{slot}\n".encode("ascii"))
        serial_connection.flush()

        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while time.monotonic() < deadline:
            response = (
                serial_connection.readline()
                .decode("utf-8", "replace")
                .strip()
            )
            if not response:
                continue

            fields = response.split(",", 2)
            if len(fields) >= 2 and fields[0] == "DONE":
                if fields[1] != str(slot):
                    raise RuntimeError(
                        f"Respons slot tidak sesuai: {response}"
                    )
                return
            if fields[0] == "ERROR":
                detail = fields[1] if len(fields) > 1 else "UNKNOWN"
                raise RuntimeError(f"Arduino menolak perintah: {detail}")

        raise TimeoutError(
            f"Tidak menerima konfirmasi dispenser untuk slot {slot}."
        )
    except (OSError, serial.SerialException, RuntimeError, TimeoutError):
        disconnect()
        raise


def check_ready():
    """Confirm the controller is responsive before creating a live payment."""
    if serial_connection is None or not serial_connection.is_open:
        raise RuntimeError("Kontroler Arduino belum terhubung.")

    try:
        serial_connection.write(b"PING\n")
        serial_connection.flush()

        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while time.monotonic() < deadline:
            response = (
                serial_connection.readline()
                .decode("utf-8", "replace")
                .strip()
            )
            if response == "PONG":
                return
            if response.startswith("ERROR"):
                raise RuntimeError(f"Arduino menolak pemeriksaan: {response}")

        raise TimeoutError("Arduino tidak merespons pemeriksaan PING.")
    except (OSError, serial.SerialException, RuntimeError, TimeoutError):
        disconnect()
        raise


def disconnect():
    """Close the current serial connection if one is open."""
    global serial_connection

    connection = serial_connection
    serial_connection = None
    if connection is not None and connection.is_open:
        connection.close()


def is_connected():
    """Return whether the serial connection is open."""
    return serial_connection is not None and serial_connection.is_open
