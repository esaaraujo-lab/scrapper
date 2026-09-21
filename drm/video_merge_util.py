"""
Video merge utility for handling DRM-protected DASH video fragments.
Supports both yt-dlp's format (.fvideo-.mp4, .faudio-.mp4) and plain segment files.
"""

import os
import subprocess
import logging
import glob
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Try to import MP4Box (optional, but recommended for DRM)
try:
    import mp4box
    HAS_MP4BOX = True
except ImportError:
    HAS_MP4BOX = False


def get_ffmpeg_path() -> str:
    """Get FFmpeg executable path."""
    try:
        result = subprocess.run(['which', 'ffmpeg'], 
                              capture_output=True, 
                              text=True, 
                              timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    
    # Fallback to 'ffmpeg' in PATH
    return 'ffmpeg'


def find_fragment_files(directory: str) -> tuple:
    """
    Find fragment files in directory.
    Returns: (video_fragments, audio_fragments, segment_files)
    """
    video_fragments = sorted(glob.glob(os.path.join(directory, '*.fvideo-*.mp4')))
    audio_fragments = sorted(glob.glob(os.path.join(directory, '*.faudio-*.mp4')))
    segment_files = sorted(glob.glob(os.path.join(directory, '[0-9]*.m4s')))
    
    return video_fragments, audio_fragments, segment_files


def merge_video_audio_ffmpeg(video_file: str, 
                             audio_file: str, 
                             output_file: str) -> bool:
    """Merge video and audio streams using ffmpeg."""
    try:
        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg, '-y',
            '-i', video_file,
            '-i', audio_file,
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-shortest',
            output_file
        ]
        
        result = subprocess.run(cmd, 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✓ Merged video+audio: {os.path.basename(output_file)}")
            return True
        else:
            logger.warning(f"FFmpeg merge failed: {result.stderr.decode()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Video+audio merge error: {e}")
        return False


def merge_segments_ffmpeg(segment_files: List[str], 
                          output_file: str) -> bool:
    """Merge .m4s segments using ffmpeg concat."""
    try:
        ffmpeg = get_ffmpeg_path()
        
        # Create concat demuxer file
        concat_file = os.path.join(os.path.dirname(output_file), 'segments.txt')
        with open(concat_file, 'w') as f:
            for seg in segment_files:
                f.write(f"file '{os.path.abspath(seg)}'\n")
        
        cmd = [
            ffmpeg, '-y', '-f', 'concat', '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',
            output_file
        ]
        
        result = subprocess.run(cmd,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✓ Merged {len(segment_files)} segments: {os.path.basename(output_file)}")
            os.remove(concat_file)
            return True
        else:
            logger.warning(f"Segment concat failed: {result.stderr.decode()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Segment merge error: {e}")
        return False


def merge_if_needed(temp_dir: str) -> bool:
    """
    Main merge function. Detects fragment types and merges appropriately.
    
    Handles:
    - yt-dlp format: .fvideo-*.mp4 + .faudio-*.m4a (CORRIGIDO!)
    - Raw DASH segments: *.m4s files
    - Already merged: single .mp4 file
    """
    
    if not os.path.isdir(temp_dir):
        logger.warning(f"Directory not found: {temp_dir}")
        return False
    
    # Get base name (output file)
    basename = os.path.basename(temp_dir.rstrip(os.sep))
    output_file = os.path.join(temp_dir, f"{basename}.mp4")
    
    # If output already exists and is valid, we're done
    if os.path.exists(output_file) and os.path.getsize(output_file) > 1024 * 1024:
        logger.debug(f"Output file already exists: {os.path.basename(output_file)}")
        return True
    
    # Find fragments - IMPORTANTE: procurar também por .m4a (áudio)
    video_frags = sorted(glob.glob(os.path.join(temp_dir, '*.fvideo-*.mp4')))
    audio_frags = sorted(glob.glob(os.path.join(temp_dir, '*.faudio-*.m4a'))) + \
                  sorted(glob.glob(os.path.join(temp_dir, '*.faudio-*.mp4')))
    segments = sorted(glob.glob(os.path.join(temp_dir, '[0-9]*.m4s')))
    
    logger.debug(f"Fragments found - Video: {len(video_frags)}, Audio: {len(audio_frags)}, Segments: {len(segments)}")
    
    # Case 1: yt-dlp format with separate video/audio (CORRIGIDO!)
    if video_frags and audio_frags:
        logger.info(f"✓ Encontrado {len(video_frags)} vídeo + {len(audio_frags)} áudio fragmentos")
        
        # Merge video fragments if more than 1
        video_merged = video_frags[0]
        if len(video_frags) > 1:
            video_merged = os.path.join(temp_dir, f"{basename}_video_merged.mp4")
            try:
                # Usar FFmpeg concat para mesclar vídeos
                concat_file = os.path.join(temp_dir, 'video_concat.txt')
                with open(concat_file, 'w') as f:
                    for vf in video_frags:
                        f.write(f"file '{os.path.abspath(vf)}'\n")
                
                result = subprocess.run(
                    ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                     '-i', concat_file, '-c', 'copy', video_merged],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300
                )
                
                if result.returncode != 0 or not os.path.exists(video_merged):
                    logger.warning(f"Falha ao mesclar vídeos, usando primeiro fragmento")
                    video_merged = video_frags[0]
                else:
                    logger.info(f"✓ {len(video_frags)} fragmentos de vídeo mesclados")
            except Exception as e:
                logger.warning(f"Erro ao mesclar vídeos: {e}")
                video_merged = video_frags[0]
        
        # Merge audio fragments if more than 1
        audio_merged = audio_frags[0]
        if len(audio_frags) > 1:
            audio_merged = os.path.join(temp_dir, f"{basename}_audio_merged.m4a")
            try:
                concat_file = os.path.join(temp_dir, 'audio_concat.txt')
                with open(concat_file, 'w') as f:
                    for af in audio_frags:
                        f.write(f"file '{os.path.abspath(af)}'\n")
                
                result = subprocess.run(
                    ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                     '-i', concat_file, '-c', 'copy', audio_merged],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300
                )
                
                if result.returncode != 0 or not os.path.exists(audio_merged):
                    logger.warning(f"Falha ao mesclar áudio, usando primeiro fragmento")
                    audio_merged = audio_frags[0]
                else:
                    logger.info(f"✓ {len(audio_frags)} fragmentos de áudio mesclados")
            except Exception as e:
                logger.warning(f"Erro ao mesclar áudio: {e}")
                audio_merged = audio_frags[0]
        
        # CRUCIAL: Mesclar vídeo + áudio
        if video_merged and audio_merged:
            logger.info(f"✓ Mesclando vídeo+áudio em: {os.path.basename(output_file)}")
            return merge_video_audio_ffmpeg(video_merged, audio_merged, output_file)
        else:
            logger.warning("Falha ao encontrar arquivos de vídeo ou áudio")
            return False
    
    # Case 2: Raw .m4s segments
    elif segments:
        logger.info(f"Found {len(segments)} .m4s segments")
        return merge_segments_ffmpeg(segments, output_file)
    
    # Case 3: Check for plain .mp4 files (already downloaded)
    else:
        mp4_files = sorted(glob.glob(os.path.join(temp_dir, '[0-9]*.mp4')))
        if mp4_files:
            logger.info(f"Found {len(mp4_files)} plain .mp4 segments")
            return merge_segments_ffmpeg(mp4_files, output_file)
        
        logger.debug(f"No fragments found in {temp_dir}")
        return False


if __name__ == "__main__":
    # Test
    import sys
    if len(sys.argv) > 1:
        test_dir = sys.argv[1]
        logging.basicConfig(level=logging.DEBUG)
        success = merge_if_needed(test_dir)
        print(f"Merge result: {success}")