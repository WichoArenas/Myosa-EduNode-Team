import psycopg2
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ================= CONFIGURATION =================
DB_HOST = "localhost"
DB_NAME = "myosa_telemetry"
DB_USER = "postgres"
DB_PASS = "wicho"


# =================================================

def generate_professional_report():
    try:
        # Conectar a la base de datos
        conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
        # Extraer los últimos 300 puntos para que la gráfica no esté saturada
        query = "SELECT timestamp, temperature, ai_temp, cpu_temp, est_current FROM rack_metrics ORDER BY timestamp DESC LIMIT 300"
        df = pd.read_sql_query(query, conn)
        conn.close()

        # Ordenar cronológicamente (de más antiguo a más reciente)
        df = df.sort_values('timestamp')

        # Estilo profesional
        plt.style.use('seaborn-v0_8-whitegrid')
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), dpi=300, sharex=True)
        fig.suptitle('EduNode GCS: Thermal Diagnostics & AI Forecast', fontsize=16, fontweight='bold')

        # --- Gráfica 1: Temperatura vs Predicción de IA ---
        ax1.plot(df['timestamp'], df['temperature'], color='#e74c3c', linewidth=2, label='Core Temp (°C)')
        ax1.plot(df['timestamp'], df['ai_temp'], color='#2ecc71', linestyle='--', linewidth=2,
                 label='AI Forecast (+15s)')
        ax1.set_ylabel('Temperature (°C)', fontweight='bold')
        ax1.legend(loc='upper left')
        ax1.set_title('Edge AI Predictive Cooling Capability', fontsize=12)

        # --- Gráfica 2: Salud del CPU y Consumo de Corriente ---
        ax2.plot(df['timestamp'], df['cpu_temp'], color='#9b59b6', linewidth=2, label='ESP32 CPU Die (°C)')
        ax2.set_ylabel('CPU Temp (°C)', color='#9b59b6', fontweight='bold')

        # Eje gemelo para los miliamperios
        ax2_twin = ax2.twinx()
        ax2_twin.plot(df['timestamp'], df['est_current'], color='#f39c12', linestyle=':', linewidth=2,
                      label='System Draw (mA)')
        ax2_twin.set_ylabel('Current (mA)', color='#f39c12', fontweight='bold')

        ax2.set_title('RTOS Multi-Core Power Efficiency', fontsize=12)

        # Unir las leyendas de ambos ejes en la Gráfica 2
        lines, labels = ax2.get_legend_handles_labels()
        lines2, labels2 = ax2_twin.get_legend_handles_labels()
        ax2.legend(lines + lines2, labels + labels2, loc='upper left')

        # --- FORMATO DEL EJE X (TIEMPO) ---
        # NUEVO: Etiqueta explícita para el eje X
        ax2.set_xlabel('Timeline (HH:MM:SS)', fontweight='bold', fontsize=11)

        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        plt.xticks(rotation=45)

        # Ajustar el diseño para que las etiquetas no se corten
        plt.tight_layout()
        # Añadir un pequeño margen inferior extra por la rotación de los números
        plt.subplots_adjust(bottom=0.15)

        # Guardar imagen en alta resolución
        plt.savefig('MYOSA_Final_Graph.png', dpi=300, bbox_inches='tight')
        print("¡Éxito! Gráfica profesional guardada como 'MYOSA_Final_Graph.png'")

    except Exception as e:
        print(f"Error generando reporte: {e}")


if __name__ == "__main__":
    generate_professional_report()