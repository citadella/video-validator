# Video Validator

A Flask-based web application for validating video files using FFmpeg. Supports both movies and TV shows with different validation checkpoints.

## Features

- Validates video files at specific timestamps (1, 10, 30 min for movies; 1, 5, 10 min for TV)
- Web-based dashboard with statistics
- Incremental scanning (only checks new, changed, or failed files)
- Paginated results view
- Automatic cleanup of deleted files

## Requirements

- Docker and Docker Compose
- FFmpeg
- Python 3.11+

## Installation

1. Clone this repository
2. Copy `docker-compose.yml` to your desired location
3. Update volume paths for your media directories
4. Run `docker-compose up -d`

## Usage

- Access the web interface at `http://your-server:8099`
- Click "Start Scan" to begin validation
- View results in the Results tab

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request
