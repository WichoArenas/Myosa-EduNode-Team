# Authors: Lixin Tu & Luis Arenas
# GCS Dashboard for EduNode Team
# Myosa Board


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import socket
import serial
import serial.tools.list_ports
import threading
import time
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from datetime import datetime
import pandas as pd
from plyer import notification
import sv_ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib import style
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ================= CONFIGURATION =================
ESP32_IP = "192.168.1.73"
ESP32_PORT = 80
BAUD_RATE = 115200

DB_HOST = "localhost"
DB_NAME = "myosa_telemetry"
DB_USER = "postgres"
DB_PASS = "wicho"
# =================================================

class MyosaDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("EduNode GCS - Tactical Telemetry Dashboard")
        self.root.geometry("1100x900")

        sv_ttk.set_theme("dark")

        self.db_conn = None
        self.connection_mode = tk.StringVar(value="WIFI_LOCAL (Lab IP)")
        self.active_mode_str = "WIFI_LOCAL (Lab IP)"
        self.usb_port_str = tk.StringVar()

        self.last_values = {'t': 0, 'h': 0, 'tx': 0, 'ty': 0, 'lux': 0, 'fan': 0}
        self.notification_cooldown = 0
        self.temp_notif_cooldown = 0

        self.setup_database()
        self.create_header()
        self.create_tabs()

        self.running = True
        self.network_thread = threading.Thread(target=self.fetch_telemetry)
        self.network_thread.daemon = True
        self.network_thread.start()

        self.auto_update_graph()

    def setup_database(self):
        try:
            init_conn = psycopg2.connect(host=DB_HOST, database="postgres", user=DB_USER, password=DB_PASS)
            init_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cursor = init_conn.cursor()
            cursor.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{DB_NAME}'")
            if not cursor.fetchone():
                cursor.execute(f"CREATE DATABASE {DB_NAME}")
            cursor.close()
            init_conn.close()
        except:
            pass

        try:
            self.db_conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
            cursor = self.db_conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rack_metrics (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    temperature FLOAT,
                    humidity FLOAT,
                    tilt_x FLOAT,
                    tilt_y FLOAT,
                    lux INTEGER,
                    fan_duty INTEGER,
                    warning BOOLEAN,
                    temp_category VARCHAR(20)
                )
            ''')
            # FIX: Dynamically add missing columns to prevent DB lockups
            try:
                cursor.execute("ALTER TABLE rack_metrics ADD COLUMN IF NOT EXISTS cpu_temp FLOAT")
                cursor.execute("ALTER TABLE rack_metrics ADD COLUMN IF NOT EXISTS ai_temp FLOAT")
                cursor.execute("ALTER TABLE rack_metrics ADD COLUMN IF NOT EXISTS est_current FLOAT")
            except:
                pass

            self.db_conn.commit()
        except Exception as e:
            print(f"DB Error: {e}")

    def create_header(self):
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=10)

        ttk.Label(header, text="🔗 Target:", font=("Segoe UI", 10, "bold")).pack(side="left", padx=5)
        self.conn_combo = ttk.Combobox(header, textvariable=self.connection_mode, state="readonly", width=25)
        self.conn_combo['values'] = ("WIFI_LOCAL (Lab IP)", "WIFI_HOTSPOT (EduNode_AP)", "USB_SERIAL (Direct Cable)")
        self.conn_combo.current(0)
        self.conn_combo.pack(side="left", padx=5)
        self.conn_combo.bind("<<ComboboxSelected>>", self.on_mode_change)

        ttk.Label(header, text="USB Port:", font=("Segoe UI", 10)).pack(side="left", padx=(20, 5))
        self.port_combo = ttk.Combobox(header, textvariable=self.usb_port_str, state="readonly", width=15)
        self.port_combo.pack(side="left", padx=5)
        self.port_combo.bind("<Button-1>", self.refresh_ports)
        self.refresh_ports()

    def refresh_ports(self, event=None):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports
        if ports and not self.port_combo.get():
            self.port_combo.set(ports[0])

    def on_mode_change(self, event=None):
        self.active_mode_str = self.connection_mode.get()

    def create_tabs(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(expand=True, fill="both", padx=10, pady=5)
        self.tab_live = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_live, text=" 📡 Live Telemetry & Control ")
        self.build_live_tab()
        self.tab_analytics = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_analytics, text=" 📊 Dual-Screen Analytics ")
        self.build_analytics_tab()
        self.update_date_list()

    def build_live_tab(self):
        self.timestamp_var = tk.StringVar(value="Last Synchronized: Waiting...")
        self.temp_var = tk.StringVar(value="-- °C")
        self.temp_cat_var = tk.StringVar(value="[STATUS]")
        self.hum_var = tk.StringVar(value="Humidity: -- %")
        self.tilt_x_var = tk.StringVar(value="Tilt X: --°")
        self.tilt_y_var = tk.StringVar(value="Tilt Y: --°")
        self.lux_var = tk.StringVar(value="Ambient Light: -- Lx")
        self.fan_var = tk.StringVar(value="Thermal Actuator: --%")
        self.alert_var = tk.StringVar(value="SYSTEM: ESTABLISHING UPLINK...")

        self.cpu_temp_var = tk.StringVar(value="CPU Die Health: -- °C")
        self.ai_pred_var = tk.StringVar(value="AI Forecast (15s): -- °C")
        self.current_var = tk.StringVar(value="Current Draw: -- mA")

        self.tgt_var = tk.IntVar(value=30)
        self.mute_var = tk.BooleanVar(value=False)
        self.os_notif_var = tk.BooleanVar(value=False)
        self.gyro_alarm_var = tk.BooleanVar(value=False)
        self.gyro_status_var = tk.StringVar(value="[DISARMED]")

        hud_frame = ttk.LabelFrame(self.tab_live, text=" Primary Flight Display (Core 0) ")
        hud_frame.pack(fill="x", padx=20, pady=10)

        ttk.Label(hud_frame, textvariable=self.timestamp_var, font=("Consolas", 10, "italic")).pack(anchor="e", padx=10, pady=2)

        temp_frame = ttk.Frame(hud_frame)
        temp_frame.pack(fill="x", pady=5)
        ttk.Label(temp_frame, text="CORE TEMP:", font=("Consolas", 14)).pack(side="left", padx=10)
        ttk.Label(temp_frame, textvariable=self.temp_var, font=("Consolas", 24, "bold"), foreground="#e74c3c").pack(side="left", padx=10)
        self.lbl_cat = tk.Label(temp_frame, textvariable=self.temp_cat_var, font=("Consolas", 14, "bold"), bg="#1e1e1e")
        self.lbl_cat.pack(side="left", padx=20)

        ttk.Label(temp_frame, textvariable=self.fan_var, font=("Consolas", 16, "bold"), foreground="#3498db").pack(side="right", padx=30)

        sys_frame = ttk.Frame(hud_frame)
        sys_frame.pack(fill="x", pady=5)
        ttk.Label(sys_frame, textvariable=self.cpu_temp_var, font=("Consolas", 12), foreground="#9b59b6").pack(side="left", padx=10)
        ttk.Label(sys_frame, textvariable=self.current_var, font=("Consolas", 12, "bold"), foreground="#f1c40f").pack(side="left", padx=30)
        ttk.Label(sys_frame, textvariable=self.ai_pred_var, font=("Consolas", 12, "bold"), foreground="#2ecc71").pack(side="right", padx=30)

        metrics_frame = ttk.LabelFrame(hud_frame, text=" Live Sensor Data ")
        metrics_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(metrics_frame, textvariable=self.hum_var, font=("Consolas", 12)).grid(row=0, column=0, sticky="w", padx=10, pady=5)
        ttk.Label(metrics_frame, textvariable=self.lux_var, font=("Consolas", 12)).grid(row=0, column=1, sticky="w", padx=40, pady=5)
        ttk.Label(metrics_frame, textvariable=self.tilt_x_var, font=("Consolas", 12)).grid(row=1, column=0, sticky="w", padx=10, pady=5)
        ttk.Label(metrics_frame, textvariable=self.tilt_y_var, font=("Consolas", 12)).grid(row=1, column=1, sticky="w", padx=40, pady=5)

        alert_frame = ttk.Frame(hud_frame)
        alert_frame.pack(fill="x", pady=10)
        self.alert_label = tk.Label(alert_frame, textvariable=self.alert_var, font=("Consolas", 14, "bold"), bg="#1e1e1e", fg="#f39c12")
        self.alert_label.pack(side="left", padx=10)

        self.gyro_status_label = tk.Label(alert_frame, textvariable=self.gyro_status_var, font=("Consolas", 12, "bold"), bg="#1e1e1e", fg="gray")
        self.gyro_status_label.pack(side="right", padx=20)

        ctrl_frame = ttk.LabelFrame(self.tab_live, text=" Command Operations (Core 1) ")
        ctrl_frame.pack(fill="x", padx=20, pady=10)

        slider_frame = ttk.Frame(ctrl_frame)
        slider_frame.pack(fill="x", pady=15)
        ttk.Label(slider_frame, text="Set Target Temp (°C):").pack(side="left", padx=10)

        self.tgt_spin = ttk.Spinbox(slider_frame, from_=20, to=40, textvariable=self.tgt_var, width=5, font=("Consolas", 12))
        self.tgt_spin.pack(side="left", padx=15)
        ttk.Button(slider_frame, text="Transmit Setpoint", command=self.update_target).pack(side="left", padx=10)

        ttk.Checkbutton(slider_frame, text="Arm Gyro Alarm", variable=self.gyro_alarm_var, command=self.toggle_gyro).pack(side="right", padx=10)
        ttk.Button(slider_frame, text="Calibrate Gyro (Zero)", command=lambda: self.send_command("CMD:CALIBRATE")).pack(side="right", padx=10)

        btn_frame = ttk.Frame(ctrl_frame)
        btn_frame.pack(fill="x", pady=10)
        ttk.Button(btn_frame, text="Manual Override (MAX FAN)", command=lambda: self.send_command("CMD:FAN_MAX")).pack(side="left", padx=5, expand=True)
        ttk.Button(btn_frame, text="Engage PD Auto-Pilot", command=lambda: self.send_command("CMD:FAN_AUTO")).pack(side="left", padx=5, expand=True)
        ttk.Button(btn_frame, text="Trigger Audible Alarm", command=lambda: self.send_command("CMD:BUZZ_ON")).pack(side="left", padx=5, expand=True)
        ttk.Button(btn_frame, text="Silence Alarm", command=lambda: self.send_command("CMD:BUZZ_OFF")).pack(side="left", padx=5, expand=True)

        opt_frame = ttk.Frame(ctrl_frame)
        opt_frame.pack(fill="x", pady=5)
        ttk.Checkbutton(opt_frame, text="Disable OS Notifications", variable=self.os_notif_var).pack(side="right", padx=10)
        ttk.Checkbutton(opt_frame, text="Mute Hardware Buzzer", variable=self.mute_var, command=self.toggle_mute).pack(side="right", padx=10)

    def toggle_mute(self):
        if self.mute_var.get():
            self.send_command("CMD:MUTE_ON")
        else:
            self.send_command("CMD:MUTE_OFF")

    def toggle_gyro(self):
        if self.gyro_alarm_var.get():
            self.send_command("CMD:GYRO_ON")
            self.gyro_status_var.set("[ARMED - Alert will trigger]")
            self.gyro_status_label.config(fg="#e74c3c")
        else:
            self.send_command("CMD:GYRO_OFF")
            self.gyro_status_var.set("[DISARMED]")
            self.gyro_status_label.config(fg="gray")

    def build_analytics_tab(self):
        tools_frame = ttk.Frame(self.tab_analytics)
        tools_frame.pack(fill="x", padx=10, pady=5)

        db_frame = ttk.Frame(tools_frame)
        db_frame.pack(fill="x", pady=2)
        ttk.Label(db_frame, text="Select Database Log:").pack(side="left", padx=5)
        self.date_combo = ttk.Combobox(db_frame, state="readonly", width=15)
        self.date_combo.pack(side="left", padx=5)
        self.date_combo.bind("<Button-1>", lambda e: self.update_date_list())

        ttk.Button(db_frame, text="Export CSV/Excel", command=self.export_to_excel).pack(side="right", padx=5)
        ttk.Button(db_frame, text="Purge Day Log", command=self.delete_day_data).pack(side="right", padx=5)

        filter_frame = ttk.Frame(tools_frame)
        filter_frame.pack(fill="x", pady=2)

        ttk.Label(filter_frame, text="Limit Points:").pack(side="left", padx=5)
        self.limit_combo = ttk.Combobox(filter_frame, state="readonly", width=8,
                                        values=["All", "10", "20", "30", "50", "100"])
        self.limit_combo.current(4)
        self.limit_combo.pack(side="left", padx=5)

        ttk.Label(filter_frame, text="Timeframe (HH:MM):").pack(side="left", padx=15)
        self.time_start = ttk.Entry(filter_frame, width=8)
        self.time_start.pack(side="left", padx=2)
        ttk.Label(filter_frame, text="to").pack(side="left")
        self.time_end = ttk.Entry(filter_frame, width=8)
        self.time_end.pack(side="left", padx=2)

        ttk.Button(filter_frame, text="Apply Query", command=self.plot_data).pack(side="left", padx=20)

        toggle_frame = ttk.LabelFrame(self.tab_analytics, text=" Overlay Metrics (Check to enable) ")
        toggle_frame.pack(fill="x", padx=10, pady=2)

        self.plot_temp = tk.BooleanVar(value=True)
        self.plot_hum = tk.BooleanVar(value=False)
        self.plot_lux = tk.BooleanVar(value=False)
        self.plot_fan = tk.BooleanVar(value=True)

        self.plot_cpu = tk.BooleanVar(value=True)
        self.plot_ai = tk.BooleanVar(value=True)
        self.plot_curr = tk.BooleanVar(value=True)

        ttk.Label(toggle_frame, text="Top Graph (Sensors):", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="Temp", variable=self.plot_temp).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="Hum", variable=self.plot_hum).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="Lux", variable=self.plot_lux).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="Fan", variable=self.plot_fan).pack(side="left", padx=5)

        ttk.Label(toggle_frame, text=" | Bottom Graph (System Health):", font=("Arial", 9, "bold")).pack(side="left", padx=10)
        ttk.Checkbutton(toggle_frame, text="CPU Temp", variable=self.plot_cpu).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="AI Forecast", variable=self.plot_ai).pack(side="left", padx=5)
        ttk.Checkbutton(toggle_frame, text="Est. Current", variable=self.plot_curr).pack(side="left", padx=5)

        style.use('dark_background')
        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.ax1 = self.figure.add_subplot(211)
        self.ax2 = self.figure.add_subplot(212, sharex=self.ax1)

        self.canvas = FigureCanvasTkAgg(self.figure, master=self.tab_analytics)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=5)

    def update_date_list(self):
        if self.db_conn:
            try:
                cursor = self.db_conn.cursor()
                cursor.execute("SELECT DISTINCT DATE(timestamp) FROM rack_metrics ORDER BY DATE(timestamp) DESC")
                dates = [row[0].strftime("%Y-%m-%d") for row in cursor.fetchall()]
                self.date_combo['values'] = dates
                if dates and not self.date_combo.get():
                    self.date_combo.set(dates[0])
            except:
                pass

    def delete_day_data(self):
        selected_date = self.date_combo.get()
        if not selected_date: return
        confirm = messagebox.askyesno("Warning", f"Purge all records for {selected_date}?")
        if confirm and self.db_conn:
            try:
                cursor = self.db_conn.cursor()
                cursor.execute("DELETE FROM rack_metrics WHERE DATE(timestamp) = %s", (selected_date,))
                self.db_conn.commit()
                messagebox.showinfo("Success", "Log purged.")
                self.update_date_list()
                self.ax1.clear()
                self.ax2.clear()
                self.canvas.draw()
            except:
                self.db_conn.rollback()

    def update_target(self):
        val = self.tgt_var.get()
        self.send_command(f"CMD:SET_TGT:{val}")

    def send_command(self, cmd_string):
        mode = self.active_mode_str
        if "WIFI" in mode:
            ip = "192.168.4.1" if "HOTSPOT" in mode else ESP32_IP
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect((ip, ESP32_PORT))
                    s.sendall((cmd_string + "\n").encode())
            except:
                pass
        elif "USB" in mode:
            com_port = self.usb_port_str.get()
            if com_port:
                try:
                    with serial.Serial(com_port, BAUD_RATE, timeout=1) as ser:
                        ser.write((cmd_string + "\n").encode())
                except:
                    pass

    def export_to_excel(self):
        selected_date = self.date_combo.get()
        if not selected_date: return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")],
            initialfile=f"EduNode_Log_{selected_date}.xlsx"
        )
        if not file_path: return
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT * FROM rack_metrics WHERE DATE(timestamp) = %s ORDER BY timestamp ASC",
                           (selected_date,))
            rows = cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            df = pd.DataFrame(rows, columns=cols)
            if file_path.endswith('.csv'):
                df.to_csv(file_path, index=False)
            else:
                df.to_excel(file_path, index=False)
            messagebox.showinfo("Success", "Data exported successfully.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def auto_update_graph(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.date_combo.get() == today_str or not self.date_combo.get():
            self.plot_data()
        self.root.after(5000, self.auto_update_graph)

    def plot_data(self):
        selected_date = self.date_combo.get()
        if not selected_date or not self.db_conn: return

        limit_val = self.limit_combo.get()
        t_start = self.time_start.get().strip()
        t_end = self.time_end.get().strip()

        query = "SELECT timestamp, temperature, humidity, lux, fan_duty, cpu_temp, ai_temp, est_current FROM rack_metrics WHERE DATE(timestamp) = %s"
        params = [selected_date]

        if t_start:
            query += " AND timestamp::time >= %s"
            params.append(t_start + ":00")
        if t_end:
            query += " AND timestamp::time <= %s"
            params.append(t_end + ":59")

        query += " ORDER BY timestamp DESC"
        if limit_val != "All": query += f" LIMIT {int(limit_val)}"

        try:
            cursor = self.db_conn.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            rows.reverse()

            self.ax1.clear()
            self.ax2.clear()

            for ax in self.figure.axes:
                if ax not in [self.ax1, self.ax2]: ax.remove()

            if not rows:
                self.canvas.draw()
                return

            times = [row[0].strftime("%H:%M:%S") for row in rows]
            last_idx = len(times) - 1

            ax1_twin = None
            needs_twin1 = self.plot_lux.get() or self.plot_fan.get()
            if needs_twin1:
                ax1_twin = self.ax1.twinx()

            if self.plot_temp.get():
                temps = [row[1] for row in rows]
                self.ax1.plot(times, temps, color='#ff6b6b', marker='o', markersize=3, label='Temp (°C)')
                self.ax1.text(last_idx, temps[-1], f" {temps[-1]:.1f}°C", color='#ff6b6b', fontweight='bold')

            if self.plot_hum.get():
                hums = [row[2] for row in rows]
                self.ax1.plot(times, hums, color='#9b59b6', linestyle='-.', label='Hum (%)')
                self.ax1.text(last_idx, hums[-1], f" {hums[-1]:.1f}%", color='#9b59b6', fontweight='bold')

            if self.plot_lux.get():
                luxs = [row[3] for row in rows]
                ax1_twin.plot(times, luxs, color='#1abc9c', alpha=0.7, label='Lux')
                ax1_twin.text(last_idx, luxs[-1], f" {luxs[-1]} Lx", color='#1abc9c', fontweight='bold')

            if self.plot_fan.get():
                fans = [int((row[4] / 255) * 100) for row in rows]
                ax1_twin.plot(times, fans, color='#3498db', linestyle='--', linewidth=2, label='Fan (%)')
                ax1_twin.text(last_idx, fans[-1], f" {fans[-1]}%", color='#3498db', fontweight='bold')

            self.ax1.set_ylabel("Environment Sensors", fontweight="bold")
            if needs_twin1: ax1_twin.set_ylabel("Fan / Lux", fontweight="bold")

            lines1, labels1 = self.ax1.get_legend_handles_labels()
            if needs_twin1:
                l2, lab2 = ax1_twin.get_legend_handles_labels()
                lines1 += l2
                labels1 += lab2
            if lines1: self.ax1.legend(lines1, labels1, loc="upper left", framealpha=0.7, fontsize=8)

            ax2_twin = None
            if self.plot_curr.get():
                ax2_twin = self.ax2.twinx()

            if self.plot_cpu.get():
                cpus = [row[5] if row[5] is not None else 0 for row in rows]
                self.ax2.plot(times, cpus, color='#9b59b6', marker='x', markersize=3, label='CPU Temp (°C)')
                self.ax2.text(last_idx, cpus[-1], f" {cpus[-1]:.1f}°C", color='#9b59b6', fontweight='bold')

            if self.plot_ai.get():
                ais = [row[6] if row[6] is not None else 0 for row in rows]
                self.ax2.plot(times, ais, color='#2ecc71', linestyle='-', label='AI Forecast (°C)')
                self.ax2.text(last_idx, ais[-1], f" {ais[-1]:.1f}°C", color='#2ecc71', fontweight='bold')

            if self.plot_curr.get():
                currs = [row[7] if row[7] is not None else 0 for row in rows]
                ax2_twin.plot(times, currs, color='#f1c40f', linestyle=':', linewidth=2, label='Current (mA)')
                ax2_twin.text(last_idx, currs[-1], f" {currs[-1]:.0f} mA", color='#f1c40f', fontweight='bold')

            self.ax2.set_xlabel("Timeline", fontweight="bold")
            self.ax2.set_ylabel("System Health", fontweight="bold")
            if ax2_twin: ax2_twin.set_ylabel("Current Draw (mA)", fontweight="bold")

            lines2, labels2 = self.ax2.get_legend_handles_labels()
            if ax2_twin:
                l3, lab3 = ax2_twin.get_legend_handles_labels()
                lines2 += l3
                labels2 += lab3
            if lines2: self.ax2.legend(lines2, labels2, loc="upper left", framealpha=0.7, fontsize=8)

            if len(times) > 10:
                self.ax2.set_xticks(self.ax2.get_xticks()[::max(1, len(times) // 10)])
            else:
                self.ax2.set_xticks(range(len(times)))
            self.ax2.set_xticklabels(self.ax2.get_xticklabels(), rotation=45)

            self.ax1.set_xlim(right=last_idx + (last_idx * 0.15))
            self.ax2.set_xlim(right=last_idx + (last_idx * 0.15))

            self.figure.tight_layout()
            self.canvas.draw()

        except Exception as e:
            print(f"Plot Error: {e}")
            self.db_conn.rollback()

    def get_trend_arrow(self, current, previous):
        if current > previous:
            return "⬆️"
        elif current < previous:
            return "⬇️"
        return "➖"

    def fetch_telemetry(self):
        while self.running:
            mode = self.active_mode_str
            data = ""
            try:
                if "WIFI" in mode:
                    ip = "192.168.4.1" if "HOTSPOT" in mode else ESP32_IP
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2)
                        s.connect((ip, ESP32_PORT))
                        data = s.recv(1024).decode().strip()
                elif "USB" in mode:
                    com_port = self.usb_port_str.get()
                    if com_port:
                        with serial.Serial(com_port, BAUD_RATE, timeout=1) as ser:
                            raw_data = ser.readline()
                            data = raw_data.decode('utf-8', errors='ignore').strip()

                if data and "|" in data:
                    parts = data.split("|")
                    if len(parts) >= 12:
                        t_val, h_val, tx_val, ty_val = map(float, parts[0:4])
                        lux_val, duty_val = map(int, parts[4:6])
                        warn_shake = (parts[6] == "1")
                        true_tgt = int(parts[7])  # FIX: Read the actual target from ESP32 memory
                        warn_intrusion = (parts[8] == "1")
                        c_temp = float(parts[9])
                        p_temp = float(parts[10])
                        e_curr = float(parts[11])

                        self.root.after(0, self.update_ui_safe, t_val, h_val, tx_val, ty_val, lux_val, duty_val,
                                        warn_shake, warn_intrusion, c_temp, p_temp, e_curr, true_tgt)
            except Exception:
                self.root.after(0, self.show_disconnect_safe, mode)

            time.sleep(1.0)

    def update_ui_safe(self, t_val, h_val, tx_val, ty_val, lux_val, duty_val, warn_shake, warn_intrusion, c_temp,
                       p_temp, e_curr, true_tgt):
        t_trend = self.get_trend_arrow(t_val, self.last_values['t'])
        h_trend = self.get_trend_arrow(h_val, self.last_values['h'])
        tx_trend = self.get_trend_arrow(tx_val, self.last_values['tx'])
        ty_trend = self.get_trend_arrow(ty_val, self.last_values['ty'])
        lux_trend = self.get_trend_arrow(lux_val, self.last_values['lux'])
        fan_pct = int((duty_val / 255) * 100)
        fan_trend = self.get_trend_arrow(fan_pct, self.last_values['fan'])

        self.last_values = {'t': t_val, 'h': h_val, 'tx': tx_val, 'ty': ty_val, 'lux': lux_val, 'fan': fan_pct}

        # FIX: Visual alarms are based on "true_tgt" (the actual ESP32 memory)
        if t_val < 22.0:
            cat_text, cat_color = "LOW", "#3498db"
        elif 22.0 <= t_val <= true_tgt + 2.0:
            cat_text, cat_color = "OPTIMAL", "#2ecc71"
        elif true_tgt + 2.0 < t_val <= true_tgt + 4.9:
            cat_text, cat_color = "WARNING", "#f39c12"
        else:
            cat_text, cat_color = "CRITICAL", "#e74c3c"

        self.timestamp_var.set(f"Last Synchronized: {datetime.now().strftime('%H:%M:%S')}")
        self.temp_var.set(f"{t_val} °C {t_trend}")
        self.temp_cat_var.set(f"[{cat_text}]")
        self.lbl_cat.config(fg=cat_color)

        self.hum_var.set(f"Humidity: {h_val} % {h_trend}")
        self.lux_var.set(f"Light: {lux_val} Lx {lux_trend}")
        self.tilt_x_var.set(f"Tilt X: {tx_val:.0f}° {tx_trend}")
        self.tilt_y_var.set(f"Tilt Y: {ty_val:.0f}° {ty_trend}")
        self.fan_var.set(f"Thermal Actuator: {fan_pct}% {fan_trend}")

        self.cpu_temp_var.set(f"CPU Die Health: {c_temp:.1f} °C")
        self.ai_pred_var.set(f"AI Forecast (15s): {p_temp:.1f} °C")
        self.current_var.set(f"Current Draw: {e_curr:.0f} mA")

        # --- ALARM SYSTEM TRIGGERED BY ACTUAL TARGET ---
        if t_val >= true_tgt + 5.0:
            self.alert_var.set("SYSTEM ALERT: CRITICAL THERMAL OVERLOAD")
            self.alert_label.config(fg="#e74c3c")
            if not getattr(self, 'temp_notif_cooldown', False): self.temp_notif_cooldown = 0
            if not self.os_notif_var.get() and time.time() - self.temp_notif_cooldown > 15:
                try:
                    notification.notify(title="Thermal Overload",
                                        message=f"Core Temp at {t_val}°C! Max Cooling Engaged.", timeout=5)
                    self.temp_notif_cooldown = time.time()
                except:
                    pass

        elif warn_intrusion:
            self.alert_var.set("SYSTEM ALERT: RACK INTRUSION (BOX OPEN)")
            self.alert_label.config(fg="#ff00ff")
            if not self.os_notif_var.get() and time.time() - self.notification_cooldown > 10:
                try:
                    notification.notify(title="Security Alert", message="Rack enclosure has been opened!", timeout=5)
                    self.notification_cooldown = time.time()
                except:
                    pass

        elif warn_shake:
            self.alert_var.set("SYSTEM ALERT: TIP-OVER DETECTED")
            self.alert_label.config(fg="#e74c3c")
            if not self.os_notif_var.get() and time.time() - self.notification_cooldown > 10:
                try:
                    notification.notify(title="EduNode GCS Alert", message="Physical movement detected on the rack!",
                                        timeout=5)
                    self.notification_cooldown = time.time()
                except:
                    pass

        else:
            self.alert_var.set("SYSTEM STATUS: SECURE (PD ACTIVE)")
            self.alert_label.config(fg="#2ecc71")

        if self.db_conn:
            try:
                cursor = self.db_conn.cursor()
                cursor.execute(
                    "INSERT INTO rack_metrics (timestamp, temperature, temp_category, humidity, tilt_x, tilt_y, lux, fan_duty, warning, cpu_temp, ai_temp, est_current) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (datetime.now(), t_val, cat_text, h_val, tx_val, ty_val, lux_val, duty_val, warn_shake, c_temp,
                     p_temp, e_curr)
                )
                self.db_conn.commit()
            except:
                pass

    def show_disconnect_safe(self, mode):
        self.alert_var.set(f"SYSTEM STATUS: OFFLINE ({mode})")
        self.alert_label.config(fg="gray")


if __name__ == "__main__":
    root = tk.Tk()
    app = MyosaDashboard(root)
    root.mainloop()
