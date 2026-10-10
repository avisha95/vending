"""Titik awal aplikasi vending: siapkan data, layar, lalu jalankan."""

from datetime import datetime
import os
import threading
import time
from queue import Empty, Queue
import tkinter as tk
from tkinter import messagebox

from qris_payment import (
    expiration_text,
    fetch_checkout_qr,
    parse_datetime,
)
from inventory import (
    create_product_list,
    load_stock as read_stock_file,
    save_stock as write_stock_file,
)
from cart import (
    add_to_cart,
    clear_cart,
    remove_from_cart,
)
from sumopod_client import SumoPodError
from configuration import (
    DEMO_MODE,
    SUMOPOD_CLIENT as client,
    LIVE_MODE,
    SANDBOX_MODE,
)
from machine_log import log_event
import serial_controller
from theme import (
    BG_COLOR,
    DANGER_COLOR,
    DISABLED_COLOR,
    PRIMARY_COLOR,
    SUCCESS_COLOR,
    TEXT_MUTED,
)
from vending_ui import create_ui


POLL_INTERVAL = 3.0
POLL_TIMEOUT = 900
SERIAL_PORT = os.environ.get("VENDING_SERIAL_PORT", "").strip()
SERIAL_BAUD_RATE = int(os.environ.get("VENDING_SERIAL_BAUD", "9600"))

# Pastikan environment variables dari .env ter-load
# .env dibaca oleh configuration.py saat import, tapi pastikan tersedia
if not SERIAL_PORT:
    import warnings
    warnings.warn(
        "VENDING_SERIAL_PORT not set. Sambungan Arduino akan dinonaktifkan. "
        "Setel environment variable atau sesuaikan .env file.",
        UserWarning,
    )


# 1. Product and order data
STOCK_FILE = os.environ.get('VENDING_STOCK') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "stock.json",
)

def load_stock():
    return read_stock_file(STOCK_FILE, log_event)


def save_stock():
    write_stock_file(STOCK_FILE, products, log_event)


products = create_product_list()
for name, stock_amount in load_stock().items():
    for item in products:
        if item['name'] == name:
            item['stock'] = stock_amount

order_data = {
    'items': [],
    'total_amount': 0,
    'payment_in_progress': False,
    'paid_pending_dispense': False,
    'payment_link': None,
}
dispenser_ready = False


def format_currency(value):
    return f"Rp {value:,}".replace(",", ".")


def process_live_payment(
    *,
    payment_client,
    amount,
    cancel_event,
    log_event_callback,
    post_to_ui,
    save_payment_link,
    set_status,
    show_payment_summary,
    dispense_product,
    failure_callback,
    display_qr_image,
    show_payment_link_qr,
):
    """Create a SumoPod payment link, display its QRIS, and poll for payment."""
    order_id = (
        "INV-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-" + os.urandom(2).hex()
    )
    log_event_callback(
        f"transaksi {order_id}: membuat pembayaran "
        f"{format_currency(amount)}"
    )

    try:
        payment = payment_client.create_payment(
            order_id=order_id,
            amount=amount,
            expires_in_hours=1,
        )
    except SumoPodError as exc:
        log_event_callback(
            f"transaksi {order_id}: GAGAL membuat pembayaran — {exc}"
        )
        post_to_ui(failure_callback, f"Gagal membuat pembayaran. {exc}")
        return

    payment_link = payment.get("payment_link_url")
    if not isinstance(payment_link, str) or not payment_link:
        log_event_callback(
            f"transaksi {order_id}: respons SumoPod tidak berisi tautan pembayaran"
        )
        post_to_ui(
            failure_callback,
            "Gagal membuat tautan pembayaran. Silakan coba lagi.",
        )
        return

    expiration_time = parse_datetime(payment.get("expires_at"))
    if cancel_event.is_set():
        log_event_callback(
            f"transaksi {order_id}: dibatalkan sebelum QR sempat dibuat. "
            f"Tautan {payment_link} TIDAK pernah ditampilkan ke pelanggan."
        )
        return

    save_payment_link(payment_link)
    log_event_callback(
        f"transaksi {order_id}: link dibuat {payment_link}"
        + (
            f" (berlaku sampai {expiration_time:%Y-%m-%d %H:%M:%S})"
            if expiration_time else ""
        )
    )
    post_to_ui(show_payment_summary, payment)
    post_to_ui(set_status, "Mengambil QR pembayaran...", PRIMARY_COLOR)

    qr_image = fetch_checkout_qr(payment_client, payment_link)
    if qr_image is not None:
        post_to_ui(display_qr_image, qr_image, "Scan QRIS untuk membayar")
    else:
        log_event_callback(
            f"transaksi {order_id}: QR tidak bisa diambil dari halaman checkout — "
            "tampilan jatuh ke cadangan tautan"
        )
        post_to_ui(show_payment_link_qr, payment_link)

    deadline = time.monotonic() + POLL_TIMEOUT
    last_status = None
    while not cancel_event.is_set():
        try:
            payment_detail = payment_client.get_payment_link(payment_link)
        except SumoPodError as exc:
            post_to_ui(
                set_status,
                f"Cek status gagal, mencoba lagi. {exc}",
                DANGER_COLOR,
            )
            log_event_callback(
                f"transaksi {order_id}: cek status gagal — {exc}"
            )
            cancel_event.wait(POLL_INTERVAL)
            continue

        if payment_client.is_paid(payment_detail):
            log_event_callback(
                f"transaksi {order_id}: LUNAS — produk dikeluarkan"
            )
            post_to_ui(set_status, "Pembayaran berhasil!", SUCCESS_COLOR)
            post_to_ui(dispense_product)
            return

        payment_status = payment_client.payin_status(payment_detail)
        if payment_status in ("FAILED", "EXPIRED", "CANCELLED", "CANCELED"):
            log_event_callback(
                f"transaksi {order_id}: pembayaran {payment_status}"
            )
            post_to_ui(
                failure_callback,
                f"Pembayaran {payment_status.lower()}. Silakan coba lagi.",
            )
            return

        if payment_status != last_status:
            log_event_callback(
                f"transaksi {order_id}: status {payment_status}"
            )
            last_status = payment_status
        post_to_ui(
            set_status,
            f"Menunggu pembayaran... ({payment_status})"
            + expiration_text(expiration_time),
            PRIMARY_COLOR,
        )

        if time.monotonic() >= deadline:
            log_event_callback(
                f"transaksi {order_id}: MENYERAH menunggu setelah "
                f"{POLL_TIMEOUT // 60} menit. QR MASIH HIDUP di {payment_link} — "
                "periksa manual apakah pembayaran masuk setelah ini."
            )
            post_to_ui(
                failure_callback,
                f"Waktu tunggu habis ({POLL_TIMEOUT // 60} menit). "
                "Pembayaran belum diterima.",
            )
            return
        cancel_event.wait(POLL_INTERVAL)

    save_payment_link(None)


def process_simulated_payment(
    *, cancel_event, post_to_ui, set_status, dispense_product,
    demo_mode=False,
):
    """Run the demo payment without contacting SumoPod."""
    waiting_text = (
        "Menunggu pembayaran simulasi..."
        if demo_mode else "Menunggu pembayaran..."
    )
    success_text = (
        "Pembayaran simulasi berhasil."
        if demo_mode else "Pembayaran berhasil!"
    )
    post_to_ui(set_status, waiting_text, PRIMARY_COLOR)
    if cancel_event.wait(3.0):
        return
    post_to_ui(set_status, success_text, SUCCESS_COLOR)
    if cancel_event.wait(1.0):
        return
    post_to_ui(dispense_product)


# 2. Application window and fullscreen controls
root = tk.Tk()
root.title("Mini Vending Machine")
root.geometry("1200x760")
root.minsize(360, 480)
root.configure(bg=BG_COLOR)

is_fullscreen = False


def maximize_window():
    """Maksimalkan jendela; gunakan ukuran layar jika OS tidak mendukungnya."""
    try:
        root.state("zoomed")
    except tk.TclError:
        try:
            root.attributes("-zoomed", True)
        except tk.TclError:
            size = f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0"
            root.geometry(size)


def toggle_fullscreen(_event=None):
    global is_fullscreen
    is_fullscreen = not is_fullscreen
    root.attributes("-fullscreen", is_fullscreen)
    return "break"


def exit_fullscreen(_event=None):
    global is_fullscreen
    is_fullscreen = False
    root.attributes("-fullscreen", False)


# 3. Product and cart actions
def select_product(index):
    if order_data['payment_in_progress'] or order_data['paid_pending_dispense']:
        return

    item = add_to_cart(products, order_data['items'], index)
    if item is None:
        messagebox.showwarning(
            "Stok Habis",
            f"{products[index]['name']} sedang habis.",
        )
        return

    order_data['total_amount'] += item['price']
    update_products()
    update_order()
    save_stock()
    show_notification(f"{item['name']} ditambahkan")


def remove_order_item(name):
    if order_data['payment_in_progress'] or order_data['paid_pending_dispense']:
        return

    item = remove_from_cart(products, order_data['items'], name)
    if item is None:
        return

    order_data['total_amount'] -= item['price']
    update_products()
    update_order()
    save_stock()
    show_notification(f"{name} dikurangi")


def clear_order():
    if order_data['payment_in_progress'] or order_data['paid_pending_dispense']:
        return
    if not clear_cart(products, order_data['items']):
        return

    order_data['total_amount'] = 0
    update_products()
    update_order()
    save_stock()
    show_notification("Pesanan dikosongkan")


def reset_stock():
    """Restore every product to its initial stock level."""
    if order_data['payment_in_progress'] or order_data['paid_pending_dispense']:
        return

    for item in products:
        item['stock'] = item['initial_stock']

    update_products()
    save_stock()
    show_notification("Stok dikembalikan ke nilai awal")


def update_products():
    locked = (
        order_data['payment_in_progress']
        or order_data['paid_pending_dispense']
    )
    ui['update_products'](products, locked)


def update_order():
    ui['update_order'](
        order_data['items'],
        order_data['total_amount'],
        (
            order_data['payment_in_progress']
            or order_data['paid_pending_dispense']
        ),
        remove_order_item,
        clear_order,
        format_currency,
        DEMO_MODE or not LIVE_MODE or dispenser_ready,
    )


# 4. User interface and button actions
# Connect payment callbacks after the UI has created its buttons.
payment_actions = {}


def start_qris_payment():
    payment_actions['start_payment']()


def cancel_payment():
    payment_actions['cancel_payment']()


ui = create_ui(
    root,
    products,
    {
        "select_product": select_product,
        "remove_order_item": remove_order_item,
        "clear_order": clear_order,
        'start_qris_payment': start_qris_payment,
        'cancel_payment': cancel_payment,
        'reset_stock': reset_stock,
    },
    format_currency,
    LIVE_MODE,
    DEMO_MODE,
)

clock_label = ui['clock_label']
notification_label = ui['notification_label']


def update_clock():
    clock_label.config(text=datetime.now().strftime("%d/%m/%Y  %H:%M:%S"))
    root.after(1000, update_clock)


_notif_after = None


def show_notification(message):
    """Tampilkan pesan singkat di bawah total."""
    global _notif_after

    if _notif_after is not None:
        try:
            root.after_cancel(_notif_after)
        except tk.TclError:
            pass

    notification_label.config(text="\u2713 " + message)
    _notif_after = root.after(1600, lambda: notification_label.config(text=" "))


ui_queue = Queue()
cancel_event = threading.Event()


def post_to_ui(function, *args, **kwargs):
    """Queue widget updates so worker threads never touch Tkinter directly."""
    ui_queue.put((function, args, kwargs))


def process_ui_queue():
    while True:
        try:
            function, args, kwargs = ui_queue.get_nowait()
        except Empty:
            break

        try:
            function(*args, **kwargs)
        except Exception as exc:
            function_name = getattr(function, "__name__", str(function))
            log_event(
                f"GALAT saat menjalankan {function_name}: "
                f"{type(exc).__name__}: {exc}"
            )

    root.after(50, process_ui_queue)


def set_payment_status(text, color=TEXT_MUTED):
    ui['payment_status_label'].config(text=text, fg=color)


def show_payment_summary(detail):
    fee = detail.get("fee")
    net_amount = detail.get("net_amount")
    if fee is None or net_amount is None:
        return

    set_payment_status(
        f"Menunggu pembayaran... (biaya {format_currency(fee)}, "
        f"diterima {format_currency(net_amount)})",
        PRIMARY_COLOR,
    )


def save_payment_link(link):
    order_data['payment_link'] = link


def dispense_product():
    if DEMO_MODE or not LIVE_MODE:
        status = (
            "Pembayaran sandbox selesai; tidak ada produk fisik dikeluarkan."
            if DEMO_MODE and LIVE_MODE else
            "Mode demo: tidak ada produk fisik yang dikeluarkan."
            if DEMO_MODE else "Simulasi dispenser berjalan..."
        )
        set_payment_status(status, PRIMARY_COLOR)
        root.after(2000, finish_transaction)
        return

    order_data['paid_pending_dispense'] = True
    update_order()
    set_payment_status("Produk sedang dikeluarkan...", PRIMARY_COLOR)
    threading.Thread(target=dispense_paid_order, daemon=True).start()


def dispense_paid_order():
    global dispenser_ready
    product_name = "tidak diketahui"
    try:
        for product_name in order_data['items']:
            product = next(
                (item for item in products if item['name'] == product_name),
                None,
            )
            if product is None:
                raise ValueError(f"Produk tidak ditemukan: {product_name}")
            serial_controller.dispense(product['slot'])
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        dispenser_ready = False
        detail = f"{type(exc).__name__}: {exc}"
        log_event(
            "PEMBAYARAN SUDAH DITERIMA, tetapi dispenser gagal. "
            "Jangan minta pelanggan membayar lagi; perlu pemeriksaan operator. "
            f"Produk yang sedang diproses: {product_name}. "
            + detail
        )
        post_to_ui(handle_dispense_failure, detail)
        post_to_ui(set_dispenser_status, "Tidak tersambung", DANGER_COLOR)
        return

    post_to_ui(finish_transaction)


def finish_transaction():
    item_count = len(order_data['items'])
    total_amount = order_data['total_amount']
    if DEMO_MODE and LIVE_MODE:
        log_event(
            f"pembayaran sandbox diterima untuk {item_count} butir "
            f"({format_currency(total_amount)}); "
            "mode demo: tidak ada produk fisik dikeluarkan"
        )
    elif DEMO_MODE:
        log_event(
            f"mode demo selesai: pembayaran disimulasikan untuk {item_count} "
            f"butir ({format_currency(total_amount)}); "
            "tidak ada produk fisik dikeluarkan"
        )
    elif LIVE_MODE:
        log_event(
            f"transaksi selesai: {item_count} butir dikeluarkan "
            f"({format_currency(total_amount)})"
        )
    else:
        log_event(
            f"simulasi selesai: {item_count} butir, "
            f"total {format_currency(total_amount)}; "
            "tidak ada pembayaran atau produk fisik"
        )

    order_data['items'].clear()
    order_data['total_amount'] = 0
    order_data['payment_in_progress'] = False
    order_data['paid_pending_dispense'] = False
    order_data['payment_link'] = None

    ui["cancel_button"].pack_forget()
    update_order()
    update_products()
    set_payment_status("Menunggu pembayaran...", TEXT_MUTED)
    ui['show_empty_qr']()
    ui['show_success_screen']()


def handle_dispense_failure(detail):
    order_data['payment_in_progress'] = False
    order_data['paid_pending_dispense'] = True
    order_data['payment_link'] = None
    ui["cancel_button"].pack_forget()
    update_order()
    update_products()
    set_payment_status(
        "Pembayaran diterima, tetapi dispenser bermasalah. Minta bantuan operator.",
        DANGER_COLOR,
    )
    ui['show_empty_qr']()
    messagebox.showerror(
        "Perlu bantuan operator",
        "Pembayaran sudah diterima, tetapi produk belum terkonfirmasi keluar.\n\n"
        "Jangan lakukan pembayaran lagi. Hubungi operator dan periksa log "
        f"mesin.\n\nDetail: {detail}",
    )


def handle_payment_failure(message):
    log_event(f"transaksi gagal: {message}")

    order_data['payment_in_progress'] = False
    order_data['payment_link'] = None

    ui["pay_button"].config(state="normal", bg=SUCCESS_COLOR)
    ui["cancel_button"].pack_forget()
    set_payment_status(message, DANGER_COLOR)
    ui['show_empty_qr']()
    update_products()
    update_order()


def handle_dispenser_check_failure(detail):
    global dispenser_ready
    dispenser_ready = False
    order_data['payment_in_progress'] = False
    order_data['payment_link'] = None
    ui["cancel_button"].pack_forget()
    set_dispenser_status("Tidak tersambung", DANGER_COLOR)
    set_payment_status(
        "Arduino tidak merespons. Pembayaran belum dimulai; periksa koneksi.",
        DANGER_COLOR,
    )
    update_products()
    update_order()
    messagebox.showerror(
        "Arduino tidak merespons",
        "Pembayaran belum dimulai. Periksa kabel dan port Arduino, lalu "
        "jalankan ulang aplikasi.\n\n"
        f"Detail: {detail}",
    )


def run_live_payment():
    process_live_payment(
        payment_client=client,
        amount=order_data['total_amount'],
        cancel_event=cancel_event,
        log_event_callback=log_event,
        post_to_ui=post_to_ui,
        save_payment_link=save_payment_link,
        set_status=set_payment_status,
        show_payment_summary=show_payment_summary,
        dispense_product=dispense_product,
        failure_callback=handle_payment_failure,
        display_qr_image=ui['display_qr_image'],
        show_payment_link_qr=ui['show_payment_link_qr'],
    )


def run_simulated_payment():
    process_simulated_payment(
        cancel_event=cancel_event,
        post_to_ui=post_to_ui,
        set_status=set_payment_status,
        dispense_product=dispense_product,
        demo_mode=DEMO_MODE,
    )


def start_payment():
    if order_data['payment_in_progress']:
        return
    if LIVE_MODE and not DEMO_MODE and not dispenser_ready:
        messagebox.showwarning(
            "Dispenser belum siap",
            "Pembayaran live hanya dapat dimulai setelah Arduino terhubung "
            "dan merespons pemeriksaan PING.",
        )
        return
    if order_data['total_amount'] == 0:
        messagebox.showinfo(
            "Pilih Produk",
            "Silakan pilih produk terlebih dahulu.",
        )
        return

    order_data['payment_in_progress'] = True
    order_data['payment_link'] = None
    cancel_event.clear()

    ui["pay_button"].config(state="disabled", bg=DISABLED_COLOR)
    ui["cancel_button"].pack(fill="x", pady=(0, 8), ipady=5)
    set_payment_status("Menyiapkan pembayaran...", PRIMARY_COLOR)
    update_products()
    update_order()

    if LIVE_MODE and not DEMO_MODE:
        set_payment_status("Memeriksa sambungan Arduino...", PRIMARY_COLOR)
        threading.Thread(target=verify_dispenser_then_pay, daemon=True).start()
    elif LIVE_MODE:
        threading.Thread(target=run_live_payment, daemon=True).start()
    else:
        threading.Thread(target=run_simulated_payment, daemon=True).start()


def verify_dispenser_then_pay():
    global dispenser_ready
    try:
        serial_controller.check_ready()
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log_event(f"Arduino tidak siap sebelum pembayaran: {detail}")
        post_to_ui(handle_dispenser_check_failure, detail)
        return

    dispenser_ready = True
    if not cancel_event.is_set():
        run_live_payment()


def cancel_payment():
    """Stop polling; a created QR remains active until it expires."""
    if not order_data['payment_in_progress']:
        return

    payment_link = order_data['payment_link']
    if payment_link:
        log_event(
            "transaksi DIBATALKAN pengguna. QR MASIH HIDUP di "
            f"{payment_link} — periksa manual apakah pembayaran masuk setelah ini."
        )
    else:
        log_event(
            "transaksi DIBATALKAN pengguna sebelum tautan pembayaran "
            "selesai dibuat"
        )

    cancel_event.set()
    order_data['payment_in_progress'] = False
    order_data['payment_link'] = None

    ui["pay_button"].config(state="normal", bg=SUCCESS_COLOR)
    ui["cancel_button"].pack_forget()
    set_payment_status(
        "Pembayaran dibatalkan. Link pembayaran masih aktif "
        "sampai kedaluwarsa.",
        TEXT_MUTED,
    )
    ui['show_empty_qr']()
    update_products()
    update_order()


payment_actions.update({
    'start_payment': start_payment,
    'cancel_payment': cancel_payment,
})


# 5. Close and run the application
def close_application():
    if order_data['paid_pending_dispense']:
        log_event(
            "MESIN DITUTUP saat pembayaran sudah diterima tetapi pesanan "
            "belum selesai dikeluarkan. Perlu pemeriksaan operator: "
            + ", ".join(order_data['items'])
        )
    elif order_data['payment_link']:
        log_event(
            "MESIN DITUTUP saat QR pembayaran masih aktif: "
            f"{order_data['payment_link']}. Periksa transaksi secara manual."
        )
    log_event("mesin ditutup (jendela ditutup pengguna)")
    cancel_event.set()
    save_stock()
    try:
        serial_controller.disconnect()
    except OSError as exc:
        log_event(
            f"GAGAL menutup koneksi serial: {type(exc).__name__}: {exc}"
        )
    finally:
        root.destroy()


def set_dispenser_status(text, color):
    ui['update_system_status']("Arduino / Dispenser", text, color)


def connect_dispenser():
    global dispenser_ready
    if DEMO_MODE:
        set_dispenser_status("Tidak diperlukan (mode demo)", TEXT_MUTED)
        return
    if not LIVE_MODE:
        set_dispenser_status("Tidak diperlukan (simulasi)", TEXT_MUTED)
        return
    if not SERIAL_PORT:
        set_dispenser_status("Port belum diatur", TEXT_MUTED)
        set_payment_status(
            "Atur VENDING_SERIAL_PORT agar dapat menerima pembayaran live.",
            DANGER_COLOR,
        )
        return

    set_dispenser_status("Menghubungkan...", PRIMARY_COLOR)

    def connect_worker():
        global dispenser_ready
        try:
            serial_controller.connect(SERIAL_PORT, SERIAL_BAUD_RATE)
        except (OSError, TimeoutError, ValueError) as exc:
            log_event(
                f"GAGAL menghubungkan dispenser pada {SERIAL_PORT}: "
                f"{type(exc).__name__}: {exc}"
            )
            post_to_ui(set_dispenser_status, "Tidak tersambung", DANGER_COLOR)
            return

        dispenser_ready = True
        log_event(f"kontroler dispenser terhubung pada {SERIAL_PORT}")
        post_to_ui(set_dispenser_status, "Siap", SUCCESS_COLOR)
        if LIVE_MODE:
            post_to_ui(
                set_payment_status,
                "Arduino siap. Pilih produk untuk memulai pembayaran.",
                SUCCESS_COLOR,
            )
        post_to_ui(update_order)

    threading.Thread(target=connect_worker, daemon=True).start()


def log_startup():
    mode = (
        "MODE DEMO (SumoPod sandbox)"
        if DEMO_MODE and LIVE_MODE else
        "MODE DEMO (simulasi lokal)"
        if DEMO_MODE else "LIVE (SumoPod)" if LIVE_MODE else "SIMULASI"
    )
    sandbox_mode = ", sandbox" if LIVE_MODE and SANDBOX_MODE else ""
    stock_summary = ", ".join(
        f"{item['name']} {item['stock']}" for item in products
    )
    log_event(f"mesin dijalankan — mode {mode}{sandbox_mode}, stok: {stock_summary}")


root.bind("<F11>", toggle_fullscreen)
root.bind("<Escape>", exit_fullscreen)
root.protocol("WM_DELETE_WINDOW", close_application)

log_startup()
connect_dispenser()
update_clock()
update_order()
update_products()
maximize_window()
root.after(50, process_ui_queue)
root.mainloop()