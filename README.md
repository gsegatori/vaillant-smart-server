# Flask Vaillant API Server

This is a Flask-based API server that integrates with the **MyPyllant** library to provide real-time information about your home automation system, specifically for Vaillant heating systems.

## Features
- Retrieve gas consumption data
- Get and update heating zone information
- Monitor water pressure
- Adjust zone temperatures and modes
- Retrieve system-wide data

## Prerequisites
- **Python 3.12**
- A valid **Vaillant** account
- Installed dependencies from `requirements.txt`

## Installation

1. **Clone the repository:**
   ```sh
   git clone <repository-url>
   cd <repository-folder>
   ```

2. **Create a virtual environment:**
   ```sh
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```

4. **Create a `.env` file** in the project root and add the following environment variables:
   ```ini
   LOG_LEVEL=WARNING
   VAILLANT_USER=<your-vaillant-username>
   VAILLANT_PASSWORD=<your-vaillant-password>
   VAILLANT_BRAND=<your-vaillant-brand>
   VAILLANT_COUNTRY=<your-vaillant-country>
   ```

## Running the Server

### Development Mode
```sh
python app.py
```

### Production Mode
For production, use **Gunicorn** or a similar WSGI server:
```sh
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## API Endpoints

### Authentication
- **`GET /test`** - Ensures authentication is valid

### Boiler Consumption
- **`GET /boiler-consumption/<year>/<month>`** - Get gas consumption for a specific month
- **`GET /boiler-consumption-current-month`** - Get gas consumption for the current month

### Zones Management
- **`GET /zones`** - Retrieve available heating zones
- **`GET /zone-info/<index>`** - Get information about a specific heating zone
- **`GET /zone-update/<index>/<mode>`** - Update zone mode (Options: `off`, `manual`, `time_controlled`)
- **`GET /zone-set-temp/<index>/<temp>`** - Set the temperature of a specific zone

### System Information
- **`GET /get-water-pressure`** - Retrieve water pressure
- **`GET /system-info`** - Get full system information

## Logging
The log level is defined in the `.env` file using `LOG_LEVEL`. Available log levels:
- `DEBUG`
- `INFO`
- `WARNING`
- `ERROR`
- `CRITICAL`

## License
This project is licensed under the MIT License.

## Contribution
Feel free to contribute by submitting a pull request or reporting issues.

---
### Author
Developed by Giorgio Segatori.

