from flask import Flask, request, jsonify, Response, stream_with_context, make_response
from youtubesearchpython import VideosSearch
import os
import re
import time
from flask_cors import CORS
import json
from threading import Thread
import threading
import yt_dlp
from pydub import AudioSegment
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging
from pathlib import Path

# Create audios directory if it doesn't exist
os.makedirs('audios', exist_ok=True)

app = Flask(__name__)
cors = CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define the retention period in seconds (2 hours)
RETENTION_PERIOD = 2 * 60 * 60

# Configure rate limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["30 per second"],
    storage_uri="memory://",
)

@app.route('/')
def nothing():
    response = jsonify({'msg': 'Use /download or /audios/<filename>'})
    response.headers.add('Content-Type', 'application/json')
    return response

def compress_audio(file_path):
    """Compress audio file to MP3 format with 256k bitrate"""
    try:
        audio = AudioSegment.from_file(file_path)
        audio.export(file_path, format='mp3', bitrate='256k')
        logger.info(f"Compressed audio file: {file_path}")
    except Exception as e:
        logger.error(f"Error compressing audio {file_path}: {e}")
        raise

def generate(host_url, video_url):
    """Generate audio file from YouTube video URL"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'audios/%(id)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '256',
        }],
        'verbose': False,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract video info without downloading
            info_dict = ydl.extract_info(video_url, download=False)
            duration = info_dict.get('duration')

            if duration and duration <= 300:  # 5 minutes limit
                # Download the video
                info_dict = ydl.extract_info(video_url, download=True)
                audio_file_path = ydl.prepare_filename(info_dict)
                thumbnail_url = info_dict.get('thumbnail')

                file_name, file_extension = os.path.splitext(audio_file_path)
                file_name = os.path.basename(file_name)
                expiration_timestamp = int(time.time()) + RETENTION_PERIOD

                # Compress audio
                mp3_path = f"audios/{file_name}.mp3"
                compress_audio(mp3_path)
                
                # Get base URL dynamically
                base_url = request.url_root.rstrip('/')
                
                response_dict = {
                    'img': thumbnail_url,
                    'direct_link': f"{base_url}/audios/{file_name}.mp3",
                    'expiration_timestamp': expiration_timestamp
                }
                response_json = json.dumps(response_dict)
                response_bytes = response_json.encode('utf-8')
                
                with app.app_context():
                    yield response_bytes
            else:
                response_dict = {
                    'error': 'Video duration must be less than or equal to 5 minutes.'
                }
                response_json = json.dumps(response_dict)
                response_bytes = response_json.encode('utf-8')
                yield response_bytes
                
    except Exception as e:
        logger.error(f"Error processing video {video_url}: {e}")
        response_dict = {
            'error': f'Error processing video: {str(e)}'
        }
        response_json = json.dumps(response_dict)
        response_bytes = response_json.encode('utf-8')
        yield response_bytes

@app.route('/search', methods=['GET'])
@limiter.limit("5/minute", error_message="Too many requests")
def search():
    """Search for YouTube videos"""
    q = request.args.get('q')
    if q is None or len(q) == 0:
        return jsonify({'error': 'Invalid search query'})
    
    try:
        s = VideosSearch(q, limit=15)
        results = s.result()["result"]
        search_results = []
        
        for video in results:
            duration = video.get("duration", "")
            if ":" in duration:
                parts = duration.split(":")
                if len(parts) == 2:  # MM:SS format
                    try:
                        minutes, seconds = map(int, parts)
                        total_seconds = minutes * 60 + seconds
                        if total_seconds < 300:  # Less than 5 minutes
                            search_results.append({
                                'title': video["title"],
                                'url': video["link"],
                                'thumbnail': video["thumbnails"][0]["url"] if video["thumbnails"] else ""
                            })
                    except (ValueError, IndexError):
                        continue
        
        response = jsonify({'search': search_results})
        response.headers.add('Content-Type', 'application/json')
        return response
        
    except Exception as e:
        logger.error(f"Error searching videos: {e}")
        return jsonify({'error': 'Search failed'}), 500

@app.route('/download', methods=['GET'])
@limiter.limit("5/minute", error_message="Too many requests")
def download_audio():
    """Download audio from YouTube video"""
    video_url = request.args.get('video_url')
    if not video_url:
        return jsonify({'error': 'video_url parameter is required'}), 400
    
    host_url = request.base_url + '/'
    return Response(
        stream_with_context(generate(host_url, video_url)), 
        mimetype='application/json'
    )

@app.route('/audios/<path:filename>', methods=['GET'])
@limiter.limit("2/5seconds", error_message="Too many requests")
def serve_audio(filename):
    """Serve audio files with range request support"""
    root_dir = os.getcwd()
    file_path = os.path.join(root_dir, 'audios', filename)
    
    # Security check: ensure filename doesn't contain path traversal
    if '..' in filename or filename.startswith('/'):
        return make_response('Invalid filename', 400)
    
    # Check if file exists
    if not os.path.isfile(file_path):
        return make_response('Audio file not found', 404)
    
    # Get file size
    file_size = os.path.getsize(file_path)
    
    # Parse Range header
    range_header = request.headers.get('Range')
    
    if range_header:
        start_pos, end_pos = parse_range_header(range_header, file_size)
        response = make_partial_response(file_path, start_pos, end_pos, file_size)
    else:
        response = make_entire_response(file_path, file_size)
    
    # Set CORS headers
    response.headers.set('Access-Control-Allow-Origin', '*')
    response.headers.set('Access-Control-Allow-Methods', 'GET')
    response.headers.set('Content-Type', 'audio/mpeg')
    
    return response

def parse_range_header(range_header, file_size):
    """Parse HTTP Range header"""
    range_match = re.search(r'(\d+)-(\d*)', range_header)
    if not range_match:
        return 0, file_size - 1
    
    start_pos = int(range_match.group(1)) if range_match.group(1) else 0
    end_pos = int(range_match.group(2)) if range_match.group(2) else file_size - 1
    
    # Ensure valid range
    start_pos = max(0, min(start_pos, file_size - 1))
    end_pos = max(start_pos, min(end_pos, file_size - 1))
    
    return start_pos, end_pos

def make_partial_response(file_path, start_pos, end_pos, file_size):
    """Create partial content response"""
    try:
        with open(file_path, 'rb') as file:
            file.seek(start_pos)
            content_length = end_pos - start_pos + 1
            content = file.read(content_length)
        
        response = make_response(content)
        response.headers.set('Content-Range', f'bytes {start_pos}-{end_pos}/{file_size}')
        response.headers.set('Content-Length', str(content_length))
        response.status_code = 206
        
        return response
        
    except Exception as e:
        logger.error(f"Error creating partial response: {e}")
        return make_response('Error reading file', 500)

def make_entire_response(file_path, file_size):
    """Create full content response"""
    try:
        with open(file_path, 'rb') as file:
            content = file.read()
        
        response = make_response(content)
        response.headers.set('Content-Length', str(file_size))
        
        return response
        
    except Exception as e:
        logger.error(f"Error creating full response: {e}")
        return make_response('Error reading file', 500)

def delete_expired_files():
    """Delete files older than retention period"""
    current_timestamp = int(time.time())
    
    try:
        if not os.path.exists('audios'):
            return
            
        for file_name in os.listdir('audios'):
            file_path = os.path.join('audios', file_name)
            
            if (os.path.isfile(file_path) and
                current_timestamp > os.path.getmtime(file_path) + RETENTION_PERIOD):
                try:
                    os.remove(file_path)
                    logger.info(f"Deleted expired file: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")
                    
    except Exception as e:
        logger.error(f"Error in delete_expired_files: {e}")

def delete_files_task():
    """Scheduled task to delete expired files"""
    delete_expired_files()
    # Schedule next run in 100 seconds
    timer = threading.Timer(100, delete_files_task)
    timer.daemon = True
    timer.start()

def run():
    """Run the Flask application"""
    app.run(host='0.0.0.0', debug=False)

def keep_alive():
    """Keep the application alive in a separate thread"""
    t = Thread(target=run)
    t.daemon = True
    t.start()

if __name__ == '__main__':
    # Start the file cleanup task
    delete_files_task()
    keep_alive()
