from datetime import datetime
from dotenv import load_dotenv

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import csv, io, os
import requests
import json
import re

load_dotenv()
app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Función para enviar la solicitud POST a la API externa
def send_post(id_dispositivo, nombre, ruta, timestamp):
    # **CAMBIO AQUÍ: Definir la URL base de ngrok para construir el enlace.**
    # Asegúrate de que esta URL sea la actual de tu ngrok.
    # Si la URL de ngrok cambia, necesitarás actualizarla aquí o leerla de una variable de entorno.
    NGROK_BASE_URL = "https://hermit-harmless-wildly.ngrok-free.app"

    # Construir el enlace completo a la imagen
    # Usamos f-strings para mayor claridad
    enlace = f"{NGROK_BASE_URL}/verImagen/{id_dispositivo}/{nombre}"

    url = "https://api-sensores.cmasccp.cl/agregarDatos"
    # url = "http://127.0.0.1:8084/agregarDatos" # Para pruebas locales

    data = {
        "tableName": "imagenes",  # Nombre de la tabla
        "formData": {  # Datos que se insertarán en el registro
            "id_dispositivo": id_dispositivo,  # id del dispositivo asociado a la imagen
            "ruta": ruta,  # Ruta del archivo (ej. uploads/1/nombre_imagen.jpg)
            "fecha": timestamp,  # Fecha y hora del registro
            "nombre": nombre,  # Nombre del archivo (ej. 1_timestampunix.jpg)
            "enlace": enlace   # **NUEVO CAMPO: Enlace completo para ver la imagen**
        }
    }
    
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(url, data=json.dumps(data), headers=headers)
        
        if response.status_code == 201:
            print(f"Metadatos enviados exitosamente para ID {id_dispositivo}, Enlace: {enlace}")
            return {'status': 'success', 'data': response.json()}
        else:
            print(f"Error al enviar metadatos para ID {id_dispositivo}: {response.status_code} - {response.text}")
            return {'status': 'fail', 'error': response.text, 'status_code': response.status_code}
    except requests.exceptions.RequestException as e:
        print(f"Error de red al enviar metadatos para ID {id_dispositivo}: {str(e)}")
        return {'status': 'fail', 'error': str(e)}

# (El resto de tu código de Flask, incluyendo @app.route('/agregarImagen', methods=['POST'])
# y las otras funciones, se mantiene exactamente igual a la versión anterior).

@app.route('/agregarImagen', methods=['POST'])
def agregar_imagen():
    print(f"[{datetime.now()}] Recibida solicitud POST en /agregarImagen")
    print(f"[{datetime.now()}] Headers: {request.headers}")
    print(f"[{datetime.now()}] Query Parameters (request.args): {request.args}")

    if not request.data:
        print(f"[{datetime.now()}] Error: No image data received.")
        return jsonify({"error": "No image data received"}), 400

    id_dispositivo = request.args.get('id_sensor')
    filename_original_with_ext = request.args.get('filename') 

    if not id_dispositivo:
        print(f"[{datetime.now()}] Error: Missing 'id_sensor' in query parameters.")
        return jsonify({"error": "Missing 'id_sensor' in query parameters. Please provide the sensor ID."}), 400
    
    if not filename_original_with_ext:
        print(f"[{datetime.now()}] Error: Missing 'filename' in query parameters.")
        return jsonify({"error": "Missing 'filename' in query parameters. Please provide the original filename."}), 400

    if not id_dispositivo.isdigit():
        print(f"[{datetime.now()}] Error: 'id_sensor' must be a numeric value. Received: {id_dispositivo}")
        return jsonify({"error": "'id_sensor' must be a numeric value."}), 400

    print(f"[{datetime.now()}] ID de dispositivo recibido: {id_dispositivo}")
    print(f"[{datetime.now()}] Nombre de archivo original recibido: {filename_original_with_ext}")

    try:
        now = datetime.now()
        timestamp_unix = int(now.timestamp())
        timestamp_str_api = now.strftime("%Y-%m-%d %H:%M:%S")

        base_name, file_extension = os.path.splitext(filename_original_with_ext)
        filename_to_save = f"{id_dispositivo}_{timestamp_unix}{file_extension}" 

        device_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], id_dispositivo)
        if not os.path.exists(device_upload_folder):
            os.makedirs(device_upload_folder, exist_ok=True)
            print(f"[{datetime.now()}] Creada carpeta: {device_upload_folder}")

        filepath = os.path.join(device_upload_folder, filename_to_save)

        with open(filepath, 'wb') as img_file:
            img_file.write(request.data)
        print(f"[{datetime.now()}] Imagen guardada en: {filepath}")

        ruta_para_api = f"{UPLOAD_FOLDER}/{id_dispositivo}/{filename_to_save}"
        
        # Llama a send_post con los nuevos parámetros
        print(f"[{datetime.now()}] Enviando metadatos a la API externa: ID={id_dispositivo}, Nombre={filename_to_save}, Ruta={ruta_para_api}, Fecha={timestamp_str_api}")
        send_post_response = send_post(id_dispositivo, filename_to_save, ruta_para_api, timestamp_str_api)

        if send_post_response['status'] == 'success':
            print(f"[{datetime.now()}] Metadatos enviados correctamente.")
            return jsonify({"message": f"Image saved at {filepath} and metadata sent successfully.",
                            "api_response": send_post_response['data']}), 200
        else:
            print(f"[{datetime.now()}] Fallo al enviar metadatos a la API. Error: {send_post_response.get('error', 'Desconocido')}")
            return jsonify({"message": f"Image saved at {filepath}, but failed to send metadata to API.",
                            "api_error": send_post_response['error']}), 202

    except Exception as e:
        print(f"[{datetime.now()}] Error interno: {str(e)}")
        return jsonify({"error": f"Failed to process request: {str(e)}"}), 500

@app.route('/verImagenes', methods=['GET'])
def ver_imagenes():
    try:
        all_images = []
        for root, dirs, files in os.walk(app.config['UPLOAD_FOLDER']):
            for file in files:
                if file.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    relative_path = os.path.relpath(os.path.join(root, file), app.config['UPLOAD_FOLDER'])
                    all_images.append(relative_path)
        return jsonify({"imagenes": all_images}), 200
    except Exception as e:
        return jsonify({"error": f"Error al obtener las imágenes: {e}"}), 500

@app.route('/verImagen/<path:filename>', methods=['GET'])
def ver_imagen(filename):
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    except FileNotFoundError:
        return jsonify({"error": "Imagen no encontrada"}), 404

def generar_csv(data):
    if not data:
        return ''
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    for row in data:
        writer.writerow(row)
    return output.getvalue()

def build_csv(df_pivoted):
    output = io.BytesIO()
    df_pivoted.to_csv(output, index=False, encoding="utf-8-sig")
    output.seek(0)
    for line in output:
        yield line      
    output.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10084)
    #app.run(host='0.0.0.0', port=10084, debug=True)
