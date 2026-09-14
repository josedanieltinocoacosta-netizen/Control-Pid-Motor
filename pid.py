import network
import socket
import time
from machine import Pin, PWM
import json

# ============================================================
# 0. CONFIGURACIÓN WI-FI (¡Modifica esto!)
# ============================================================
WIFI_SSID = "Redmi Note 10 Pro"
WIFI_PASS = "1043964384"

# ============================================================
# 1. DRIVER L9110S
# ============================================================
class MotorL9110S:
    def __init__(self, pin_ia, pin_ib):
        self.ia = PWM(Pin(pin_ia), freq=1000, duty=0)
        self.ib = PWM(Pin(pin_ib), freq=1000, duty=0)

    def aplicar_voltaje(self, pwm_val):
        pwm_val = max(-1023, min(1023, int(pwm_val)))
        if pwm_val > 0:
            self.ia.duty(pwm_val)
            self.ib.duty(0)
        elif pwm_val < 0:
            self.ia.duty(0)
            self.ib.duty(abs(pwm_val))
        else:
            self.ia.duty(0)
            self.ib.duty(0)

# ============================================================
# 2. SENSOR OPTICO CON FILTRO EMA
# ============================================================
class EncoderLM393:
    def __init__(self, pin_out, ranuras_disco=40, alpha=0.2):
        self.pin = Pin(pin_out, Pin.IN)
        self.ranuras = ranuras_disco
        self.pulsos = 0
        self.alpha = alpha 
        self.rpm_filtrado = 0.0 
        self.pin.irq(trigger=Pin.IRQ_RISING, handler=self._contador_pulsos)

    def _contador_pulsos(self, pin):
        self.pulsos += 1

    def obtener_rpm(self, dt):
        if dt <= 0: return 0.0
        rpm_crudo = (self.pulsos / self.ranuras) * (60.0 / dt)
        self.pulsos = 0
        self.rpm_filtrado = (self.alpha * rpm_crudo) + ((1.0 - self.alpha) * self.rpm_filtrado)
        return self.rpm_filtrado

# ============================================================
# 3. CONTROLADOR PID
# ============================================================
class ControladorPID:
    def __init__(self, Kp, Ki, Kd):
        self.Kp = Kp
        self.Ki = Ki
        self.Kd = Kd
        self.integral = 0.0
        self.error_ant = 0.0

    def calcular(self, referencia, valor_medido, dt):
        error = referencia - valor_medido
        P = self.Kp * error
        self.integral += error * dt
        I = self.Ki * self.integral
        derivada = (error - self.error_ant) / dt if dt > 0 else 0
        D = self.Kd * derivada
        salida = P + I + D
        self.error_ant = error
        return salida, error

# ============================================================
# 4. CONEXIÓN WI-FI Y SERVIDOR
# ============================================================
print("Conectando a Wi-Fi...")
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(WIFI_SSID, WIFI_PASS)

while not wlan.isconnected():
    time.sleep(0.5)
    print(".", end="")

ip = wlan.ifconfig()[0]
print(f"\n¡Conectado! IP del ESP32: {ip}")
print("Pon esta IP en tu archivo HTML en la variable ESP32_IP.")

# Creamos el servidor web en el puerto 80
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 80))
s.listen(5)
s.setblocking(False) # ¡CRUCIAL! Para que el motor no se detenga esperando conexiones

# ============================================================
# 5. VARIABLES DE ESTADO GLOBALES
# ============================================================
estado_modo = 'pid'   # 'pid' o 'pwm'
target_rpm = 45.0
pwm_manual = 0
salida_pwm = 0
error_actual = 0.0

# Inicialización de Hardware (Usando tus valores Kp, Ki, Kd)
motor = MotorL9110S(pin_ia=13, pin_ib=14)
encoder = EncoderLM393(pin_out=18, ranuras_disco=40, alpha=0.2)
pid = ControladorPID(Kp=0.5, Ki=0.2, Kd=0.02)

dt = 0.5 

# ============================================================
# 6. BUCLE PRINCIPAL (PID + Servidor Web)
# ============================================================
try:
    while True:
        t_inicio = time.ticks_ms()
        
        # --- A. LECTURA DEL SENSOR ---
        rpm_reales = encoder.obtener_rpm(dt)
        
        # --- B. CONTROL DEL MOTOR ---
        if estado_modo == 'pid':
            if target_rpm > 0:
                salida_pid, error_actual = pid.calcular(target_rpm, rpm_reales, dt)
                salida_pwm = min(1023, max(0, int(salida_pid * 5))) 
            else:
                salida_pwm = 0
                error_actual = 0
                pid.integral = 0 # Reseteo de seguridad
        elif estado_modo == 'pwm':
            salida_pwm = pwm_manual
            error_actual = 0.0
            pid.integral = 0
            
        motor.aplicar_voltaje(salida_pwm)
        
        # --- C. ESCUCHAR A LA PÁGINA WEB ---
        try:
            conn, addr = s.accept()
            request = conn.recv(1024).decode('utf-8')
            
            # Buscar si llegaron variables (Ej: /?modo=pid&target=45...)
            if "GET /?" in request:
                inicio = request.find('/?') + 2
                fin = request.find(' HTTP')
                query = request[inicio:fin]
                
                parametros = query.split('&')
                for param in parametros:
                    llave, valor = param.split('=')
                    if llave == 'modo': estado_modo = valor
                    elif llave == 'target': target_rpm = float(valor)
                    elif llave == 'kp': pid.Kp = float(valor)
                    elif llave == 'ki': pid.Ki = float(valor)
                    elif llave == 'kd': pid.Kd = float(valor)
                    elif llave == 'pwm': pwm_manual = int(valor)

            # Preparamos el JSON de respuesta con los datos actuales
            telemetria = {
                "target": target_rpm if estado_modo == 'pid' else 0,
                "rpm_reales": rpm_reales,
                "error": error_actual,
                "pwm": salida_pwm
            }
            json_datos = json.dumps(telemetria)
            
            # Respuesta HTTP con CORS (Obligatorio para que funcione desde HTML local)
            response = "HTTP/1.1 200 OK\r\n"
            response += "Content-Type: application/json\r\n"
            response += "Access-Control-Allow-Origin: *\r\n" # Permite que tu HTML lo lea
            response += "Connection: close\r\n\r\n"
            response += json_datos
            
            conn.send(response.encode('utf-8'))
            conn.close()
            
        except OSError:
            pass # No hay peticiones web en este instante, el bucle sigue normalmente.

        # --- D. ESPERAR TIEMPO EXACTO ---
        t_ejecucion = time.ticks_diff(time.ticks_ms(), t_inicio) / 1000.0
        if dt > t_ejecucion:
            time.sleep(dt - t_ejecucion)

except KeyboardInterrupt:
    print("\nSistema detenido.")
    motor.aplicar_voltaje(0)
    s.close()