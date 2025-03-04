import base64
import io
import json
import os
import time
import traceback

import easyocr
import ollama
import pkg_resources
from PIL import Image
from ultralytics import YOLO

from operate.config import Config
from operate.exceptions import ModelNotRecognizedException
from operate.models.prompts import (
    get_system_prompt,
    get_user_first_message_prompt,
    get_user_prompt,
)
from operate.utils.label import (
    add_labels,
    get_click_position_in_percent,
    get_label_coordinates,
)
from operate.utils.ocr import get_text_coordinates, get_text_element
from operate.utils.screenshot import capture_screen_with_cursor
from operate.utils.screenshot import capture_screen_with_cursor, compress_screenshot
from operate.utils.style import ANSI_BRIGHT_MAGENTA, ANSI_GREEN, ANSI_RED, ANSI_RESET

# Load configuration
config = Config()


async def get_next_action(model, messages, objective, session_id):
    if config.verbose:
        print("[Self-Operating Computer][get_next_action]")
        print("[Self-Operating Computer][get_next_action] model", model)
    if model == "gpt-4":
        return call_gpt_4o(messages), None
    if model == "claude-3.7":
        return call_claude_37(messages), None
    if model == "qwen-vl":
        operation = await call_qwen_vl_with_ocr(messages, objective, model)
        return operation, None
    if model == "gpt-4-with-som":
        operation = await call_gpt_4o_labeled(messages, objective, model)
        return operation, None
    if model == "gpt-4-with-ocr":
        operation = await call_gpt_4o_with_ocr(messages, objective, model)
        return operation, None
    if model == "o1-with-ocr":
        operation = await call_o1_with_ocr(messages, objective, model)
        return operation, None
    if model == "agent-1":
        return "coming soon"
    if model == "gemini-pro-vision":
        return call_gemini_pro_vision(messages, objective), None
    if model == "llava":
        operation = call_ollama_llava(messages)
        return operation, None
    if model == "claude-3":
        operation = await call_claude_3_with_ocr(messages, objective, model)
        return operation, None
    raise ModelNotRecognizedException(model)


def call_gpt_4o(messages):
    if config.verbose:
        print("[call_gpt_4_v]")
    time.sleep(1)
    client = config.initialize_openai()
    try:
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        with open(screenshot_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        if config.verbose:
            print(
                "[call_gpt_4_v] user_prompt",
                user_prompt,
            )

        vision_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
                },
            ],
        }
        messages.append(vision_message)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            presence_penalty=1,
            frequency_penalty=1,
        )

        content = response.choices[0].message.content

        content = clean_json(content)

        assistant_message = {"role": "assistant", "content": content}
        if config.verbose:
            print(
                "[call_gpt_4_v] content",
                content,
            )
        content = json.loads(content)

        messages.append(assistant_message)

        return content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[Operate] That did not work. Trying again {ANSI_RESET}",
            e,
        )
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Error] AI response was {ANSI_RESET}",
            content,
        )
        if config.verbose:
            traceback.print_exc()
        return call_gpt_4o(messages)


def extract_target_from_text(text):
    """
    Extract target file/folder names from text with intelligent priority.

    Args:
        text (str): Text to analyze (thought or operation text)

    Returns:
        str: The extracted target description
    """
    import re

    # Priority 1: Look for quoted text which often indicates file/folder names
    quoted_pattern = re.compile(r"['\"]([^'\"]+)['\"]")
    quoted_matches = quoted_pattern.findall(text)
    if quoted_matches:
        return quoted_matches[0]

    # Priority 2: Look for file/folder patterns (word-word or words with extensions)
    file_pattern = re.compile(r"(\w+[-\.]\w+[-\.]\w+|\w+[-\.]\w+)")
    file_matches = file_pattern.findall(text)
    for match in file_matches:
        # Filter out things that don't look like folder/file names
        if any(x in match.lower() for x in ['-main', 'folder', 'file', 'image', 'doc', '.', 'sbc']):
            return match

    # Priority 3: Look for phrases after "click on X" or "open X"
    click_phrases = ["click on ", "click the ", "clicking on ", "clicking the ", "open ", "opening "]
    for phrase in click_phrases:
        if phrase in text.lower():
            parts = text.lower().split(phrase, 1)
            if len(parts) > 1:
                # Extract up to a period, comma, or space
                target = parts[1].split(".")[0].split(",")[0].strip()
                # Only return if it's not too long (likely not a file name if very long)
                if 2 <= len(target.split()) <= 5:
                    return target

    # Priority 4: Look for capitalized words which might be file/folder names
    cap_word_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b')
    cap_matches = cap_word_pattern.findall(text)
    if cap_matches:
        # Filter to likely file/folder names
        likely_matches = [m for m in cap_matches if len(m) > 3]
        if likely_matches:
            return likely_matches[0]

    # Default: just return the original text if nothing better found
    return text


def find_ui_element_by_text_and_vision(target_description, screenshot_filename):
    """
    Finds UI elements using multiple methods: text OCR, template matching, and shape detection.
    Specialized for finding desktop icons, folders, and common UI elements.

    Args:
        target_description (str): Description of what we're trying to find (e.g., "sbc-images-main")
        screenshot_filename (str): Path to screenshot file

    Returns:
        tuple: (x_percent, y_percent) coordinates as percentages of screen width/height, or None if not found
    """
    import cv2
    import numpy as np
    from PIL import Image
    import easyocr
    import os
    import re

    # Clean up the target description for better matching
    target_words = target_description.lower().split()
    # Remove common words that don't help with identification
    stop_words = ['the', 'a', 'an', 'to', 'on', 'in', 'by', 'it', 'this', 'that', 'for', 'with', 'click', 'double']
    target_words = [word for word in target_words if word not in stop_words]
    clean_target = ' '.join(target_words)

    print(f"[Target Finder] Looking for: '{clean_target}'")

    # Load the screenshot
    screenshot = Image.open(screenshot_filename)
    screenshot_np = np.array(screenshot)
    screenshot_rgb = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)

    # Create a debug image to visualize findings
    debug_img = screenshot_rgb.copy()

    # Results will store all potential matches with their confidence scores
    results = []

    # APPROACH 1: Template matching with saved templates
    icon_folder = "icon_templates"
    if os.path.exists(icon_folder) and any(os.listdir(icon_folder)):
        for filename in os.listdir(icon_folder):
            if filename.endswith(('.png', '.jpg')):
                # Extract the template name for matching
                template_name = filename.replace('_', ' ').replace('.png', '').replace('.jpg', '')

                # Check if template name matches any part of the target
                if any(word in template_name.lower() for word in target_words) or \
                        any(word in clean_target for word in template_name.lower().split()):

                    template_path = os.path.join(icon_folder, filename)
                    template = cv2.imread(template_path)

                    if template is None:
                        continue

                    # Apply template matching
                    res = cv2.matchTemplate(screenshot_rgb, template, cv2.TM_CCOEFF_NORMED)
                    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

                    if max_val > 0.7:  # Good match
                        template_h, template_w = template.shape[:2]
                        top_left = max_loc
                        bottom_right = (top_left[0] + template_w, top_left[1] + template_h)
                        center_x = top_left[0] + template_w // 2
                        center_y = top_left[1] + template_h // 2

                        # Add to results with high confidence since it's a template match
                        match_score = max_val * 1.5  # Boost template matches
                        results.append({
                            "type": "template",
                            "confidence": match_score,
                            "center": (center_x, center_y),
                            "bbox": (top_left[0], top_left[1], bottom_right[0], bottom_right[1])
                        })

                        # Draw on debug image
                        cv2.rectangle(debug_img, top_left, bottom_right, (0, 255, 0), 2)
                        cv2.putText(debug_img, f"Template: {template_name} ({match_score:.2f})",
                                    (top_left[0], top_left[1] - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

    # APPROACH 2: OCR text detection
    try:
        # Initialize EasyOCR Reader
        reader = easyocr.Reader(["en"])

        # Read the screenshot
        ocr_results = reader.readtext(screenshot_filename)

        for idx, (bbox, text, conf) in enumerate(ocr_results):
            text_lower = text.lower()

            # Check for any word match
            word_match = False
            for word in target_words:
                if len(word) > 2 and word in text_lower:  # Avoid matching very short words
                    word_match = True
                    break

            # Calculate match score based on text similarity
            if word_match or clean_target in text_lower or text_lower in clean_target:
                # Calculate match score
                from difflib import SequenceMatcher
                similarity = SequenceMatcher(None, clean_target, text_lower).ratio()
                match_score = similarity * conf

                # Especially boost exact matches or strong partial matches
                if similarity > 0.8:
                    match_score *= 1.5

                # Get center of text bounding box
                bbox_points = np.array(bbox).astype(int)
                center_x = np.mean([p[0] for p in bbox_points])
                center_y = np.mean([p[1] for p in bbox_points])

                # Calculate bounding box rectangle
                x_points = [p[0] for p in bbox_points]
                y_points = [p[1] for p in bbox_points]
                bbox_rect = (min(x_points), min(y_points), max(x_points), max(y_points))

                # Add to results
                results.append({
                    "type": "text",
                    "text": text,
                    "confidence": match_score,
                    "center": (center_x, center_y),
                    "bbox": bbox_rect
                })

                # Draw on debug image
                top_left = (int(bbox_rect[0]), int(bbox_rect[1]))
                bottom_right = (int(bbox_rect[2]), int(bbox_rect[3]))
                cv2.rectangle(debug_img, top_left, bottom_right, (0, 0, 255), 2)
                cv2.putText(debug_img, f"OCR: {text} ({match_score:.2f})",
                            (top_left[0], top_left[1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

                # For text results, look for potential UI elements above (desktop icon case)
                # If this looks like a desktop icon label, the actual icon is likely above it
                if any(word in text_lower for word in ['folder', 'file', 'image', 'doc']) or \
                        re.search(r'\w+[-\.]\w+', text_lower) or \
                        "sbc" in text_lower:
                    # Define a region above the text to look for the icon
                    icon_area_width = bbox_rect[2] - bbox_rect[0]
                    icon_area_height = icon_area_width  # Make it square
                    icon_area_top = max(0, bbox_rect[1] - icon_area_height - 10)  # Above text with a small gap
                    icon_area_left = bbox_rect[0]

                    icon_center_x = icon_area_left + icon_area_width // 2
                    icon_center_y = icon_area_top + icon_area_height // 2

                    # Add this as a potential icon location with boosted confidence
                    icon_match_score = match_score * 1.2  # Boost confidence for icon targets
                    results.append({
                        "type": "icon",
                        "confidence": icon_match_score,
                        "center": (icon_center_x, icon_center_y),
                        "bbox": (icon_area_left, icon_area_top,
                                 icon_area_left + icon_area_width, icon_area_top + icon_area_height)
                    })

                    # Draw the potential icon area
                    cv2.rectangle(debug_img,
                                  (int(icon_area_left), int(icon_area_top)),
                                  (int(icon_area_left + icon_area_width), int(icon_area_top + icon_area_height)),
                                  (255, 0, 0), 2)
                    cv2.putText(debug_img, f"Icon target ({icon_match_score:.2f})",
                                (int(icon_area_left), int(icon_area_top) - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)

    except Exception as e:
        print(f"[Target Finder] OCR detection error: {e}")

    # APPROACH 3: Folder icon detection (color/shape based)
    if "folder" in clean_target or "file" in clean_target or "sbc" in clean_target:
        try:
            # Convert to HSV for better color segmentation
            hsv = cv2.cvtColor(screenshot_rgb, cv2.COLOR_BGR2HSV)

            # Define color ranges for common folder icons (yellow folders in Windows)
            lower_yellow = np.array([20, 100, 100])
            upper_yellow = np.array([40, 255, 255])

            # Create mask for yellow color
            mask = cv2.inRange(hsv, lower_yellow, upper_yellow)

            # Find contours in the mask
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # Filter contours by size (folder icons are usually of similar size)
            min_area = 100
            max_area = 5000

            for contour in contours:
                area = cv2.contourArea(contour)
                if min_area < area < max_area:
                    # Get center of contour
                    M = cv2.moments(contour)
                    if M["m00"] > 0:
                        center_x = int(M["m10"] / M["m00"])
                        center_y = int(M["m01"] / M["m00"])

                        # Get bounding box
                        x, y, w, h = cv2.boundingRect(contour)

                        # Add to results with lower confidence for shape-based detection
                        match_score = 0.5  # Base confidence for shape detection
                        results.append({
                            "type": "shape",
                            "confidence": match_score,
                            "center": (center_x, center_y),
                            "bbox": (x, y, x + w, y + h)
                        })

                        # Draw on debug image
                        cv2.rectangle(debug_img, (x, y), (x + w, y + h), (255, 255, 0), 2)
                        cv2.putText(debug_img, f"Shape ({match_score:.2f})",
                                    (x, y - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 2)
        except Exception as e:
            print(f"[Target Finder] Shape detection error: {e}")

    # Save the debug image
    cv2.imwrite("debug_target_detection.jpg", debug_img)

    if results:
        # Sort by confidence
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        best_match = results[0]

        # Print debug info
        print(f"[Target Finder] Best match: {best_match['type']} with confidence {best_match['confidence']:.2f}")

        # Get the center point
        center_x, center_y = best_match["center"]

        # Convert to percentage of screen size
        screen_width, screen_height = screenshot.size
        x_percent = center_x / screen_width
        y_percent = center_y / screen_height

        # Mark the final target on the debug image
        result_img = cv2.circle(debug_img, (int(center_x), int(center_y)), 10, (0, 255, 255), -1)
        cv2.imwrite("debug_final_target.jpg", result_img)

        return (x_percent, y_percent)

    print(f"[Target Finder] No match found for '{clean_target}'")
    return None


def verify_success(screenshot_before, task_type="open_folder"):
    """
    Verifies if an operation was successful by comparing before/after screenshots.

    Args:
        screenshot_before: Screenshot taken before the operation
        task_type: Type of task we're verifying (open_folder, click_button, etc.)

    Returns:
        bool: True if operation appears successful, False otherwise
    """
    import cv2
    import numpy as np
    import pyautogui

    # Take a screenshot after the operation
    screenshot_after = pyautogui.screenshot()

    # Convert to numpy arrays for comparison
    before_np = np.array(screenshot_before)
    after_np = np.array(screenshot_after)

    # Resize if dimensions don't match
    if before_np.shape != after_np.shape:
        after_np = cv2.resize(after_np, (before_np.shape[1], before_np.shape[0]))

    # For opening a folder, check for significant window change
    if task_type == "open_folder":
        # Calculate difference between images
        diff = cv2.absdiff(before_np, after_np)
        gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, thresholded = cv2.threshold(gray_diff, 30, 255, cv2.THRESH_BINARY)

        # Calculate percentage of changed pixels
        changed_pixels = np.count_nonzero(thresholded)
        total_pixels = thresholded.size
        change_percentage = (changed_pixels / total_pixels) * 100

        # Save debug images
        cv2.imwrite("debug_before.jpg", cv2.cvtColor(before_np, cv2.COLOR_RGB2BGR))
        cv2.imwrite("debug_after.jpg", cv2.cvtColor(after_np, cv2.COLOR_RGB2BGR))
        cv2.imwrite("debug_diff.jpg", thresholded)

        print(f"[Verification] Screen change: {change_percentage:.2f}%")

        # If significant portion of screen changed, likely a new window opened
        return change_percentage > 15

    return False


def call_claude_37(messages):
    if config.verbose:
        print("[call_claude_37]")
    time.sleep(1)

    # Import all required modules
    import anthropic
    import cv2
    import numpy as np
    import re
    import pyautogui
    from PIL import Image

    try:
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)
        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")

        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        # Convert PNG to JPEG format to ensure compatibility
        img = Image.open(screenshot_filename)
        if img.mode in ('RGBA', 'LA'):
            # Remove alpha channel for JPEG compatibility
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
            img = background

        # Save as JPEG
        jpeg_filename = os.path.join(screenshots_dir, "screenshot.jpg")
        img.save(jpeg_filename, "JPEG", quality=95)

        with open(jpeg_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        # Determine which prompt to use
        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        if config.verbose:
            print("[call_claude_37] user_prompt", user_prompt)

        # Initialize Anthropic client directly with the environment variable
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            api_key = config.anthropic_api_key  # Fallback to instance variable

        if config.verbose:
            print("[call_claude_37] Using Anthropic API key (masked):", "*" * len(api_key) if api_key else "None")

        client = anthropic.Anthropic(api_key=api_key)

        # Extract system message
        system_content = None
        if messages and messages[0]["role"] == "system":
            system_content = messages[0]["content"]
            user_messages = messages[1:-1] if len(messages) > 1 else []  # Skip system message and last message
        else:
            user_messages = messages[:-1] if messages else []  # No system message, include all but last

        # Convert previous messages to Anthropic format
        anthropic_messages = []
        for msg in user_messages:
            if msg["role"] in ["user", "assistant"]:  # Only include user and assistant messages
                anthropic_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

        # Create vision message for Claude
        vision_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_base64
                    }
                }
            ]
        }

        # Add the vision message
        anthropic_messages.append(vision_message)

        if config.verbose:
            print("[call_claude_37] System content length:", len(system_content) if system_content else 0)
            print("[call_claude_37] Number of messages:", len(anthropic_messages))

        # Create the message request
        response = client.messages.create(
            model="claude-3-7-sonnet-20250219",
            messages=anthropic_messages,
            system=system_content,
            max_tokens=2048,
        )

        # Extract the content from the response
        content = response.content[0].text

        # Check if Claude added text before the JSON
        if content.strip().startswith("[") or content.strip().startswith("{"):
            # Content is already in JSON format, just clean it
            content = clean_json(content)
        else:
            # Claude might have added a message before the JSON
            # Try to find JSON in the content
            json_match = re.search(r'(\[.*\]|\{.*\})', content, re.DOTALL)
            if json_match:
                # Extract the JSON part
                content = clean_json(json_match.group(1))
            else:
                # If no JSON found, try to create a done operation
                if "done" in content.lower() or "complete" in content.lower():
                    content = '[{"thought": "Task complete", "operation": "done"}]'
                else:
                    # Create a fallback operation
                    content = '[{"thought": "Continuing task", "operation": "wait", "duration": 1}]'

        # Log the cleaned content
        if config.verbose:
            print("[call_claude_37] cleaned content", content)

        # Create assistant message with the original response
        assistant_message = {"role": "assistant", "content": response.content[0].text}

        try:
            # Try to parse as JSON
            parsed_content = json.loads(content)
            if config.verbose:
                print("[call_claude_37] Successfully parsed content as JSON")
        except json.JSONDecodeError as e:
            # If JSON parsing fails, create a simple operation
            print(f"[call_claude_37] JSON parsing failed: {e}. Creating fallback operation.")
            parsed_content = [{"thought": "Continuing with task", "operation": "wait", "duration": 1}]

        # Process the operations with enhanced handling
        processed_content = []

        # Check if Claude is trying to do a double-click
        need_double_click = False
        for operation in parsed_content:
            if operation.get("double_click", False):
                need_double_click = True
                break
            if "thought" in operation:
                if "double" in operation["thought"].lower() and "click" in operation["thought"].lower():
                    need_double_click = True
                    break

        for i, operation in enumerate(parsed_content):
            if operation.get("operation") == "click":
                # Extract target description
                target_description = ""
                if "text" in operation:
                    target_description = operation.get("text")
                elif "thought" in operation:
                    # Try to extract what we're clicking on from the thought
                    thought = operation.get("thought", "")

                    # Look for quoted text first
                    quoted_match = re.search(r'[\'"]([^\'\"]+)[\'"]', thought)
                    if quoted_match:
                        target_description = quoted_match.group(1)
                    else:
                        # Look for instances of "sbc-images-main" or similar patterns
                        pattern_match = re.search(r'(\b\w+-\w+-\w+\b|\bsbc[- ]\w+\b)', thought, re.IGNORECASE)
                        if pattern_match:
                            target_description = pattern_match.group(1)
                        else:
                            # Fall back to looking for phrases after click indicators
                            click_indicators = ["click on", "click the", "clicking on", "clicking the"]
                            for indicator in click_indicators:
                                if indicator in thought.lower():
                                    parts = thought.lower().split(indicator, 1)
                                    if len(parts) > 1:
                                        target_description = parts[1].split(".")[0].split(",")[0].strip()
                                        break

                if not target_description:
                    target_description = f"target at position ({operation['x']}, {operation['y']})"

                if config.verbose:
                    print(f"[call_claude_37] Target description: {target_description}")

                # Handle double-clicking if detected
                if need_double_click and i == 0:  # Only process the first click for double-click
                    # Extract coordinates
                    try:
                        x = operation["x"]
                        y = operation["y"]

                        # Add a special marker to signal double-click
                        operation["double_click"] = True

                        # Log the double-click intention
                        print(
                            f"[call_claude_37] Detected double-click operation on '{target_description}' at ({x}, {y})")
                    except Exception as e:
                        print(f"[call_claude_37] Error processing double-click: {e}")

                # For double-click operations, we only need to add the first click
                # Skip adding second clicks to avoid duplicate operations
                if need_double_click and i > 0:
                    if config.verbose:
                        print("[call_claude_37] Skipping duplicate click for double-click operation")
                    continue

                # Add the operation
                if config.verbose:
                    print(f"[call_claude_37] Adding operation: {operation}")

                processed_content.append(operation)
            else:
                # For non-click operations, just append as is
                processed_content.append(operation)

        # Add the assistant message to the history
        messages.append(assistant_message)

        # Return the processed content
        return processed_content if processed_content else [{"operation": "wait", "duration": 1}]

    except Exception as e:
        error_msg = str(e)
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[Operate] That did not work. Trying again {ANSI_RESET}",
            error_msg,
        )

        # Define content_str before using it to avoid the "referenced before assignment" error
        content_str = "No content received"
        if 'content' in locals():
            content_str = content

        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Error] AI response was {ANSI_RESET}",
            content_str,
        )

        if config.verbose:
            traceback.print_exc()

        # If an exception occurs, return a simple operation to keep things moving
        return [{"thought": "Continuing task after error", "operation": "wait", "duration": 1}]
async def call_qwen_vl_with_ocr(messages, objective, model):
    if config.verbose:
        print("[call_qwen_vl_with_ocr]")

    # Construct the path to the file within the package
    try:
        time.sleep(1)
        client = config.initialize_qwen()

        confirm_system_prompt(messages, objective, model)
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        # Call the function to capture the screen with the cursor
        raw_screenshot_filename = os.path.join(screenshots_dir, "raw_screenshot.png")
        capture_screen_with_cursor(raw_screenshot_filename)

        # Compress screenshot image to make size be smaller
        screenshot_filename = os.path.join(screenshots_dir, "screenshot.jpeg")
        compress_screenshot(raw_screenshot_filename, screenshot_filename)

        with open(screenshot_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        vision_message = {
            "role": "user",
            "content": [
                {"type": "text",
                 "text": f"{user_prompt}**REMEMBER** Only output json format, do not append any other text."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
                },
            ],
        }
        messages.append(vision_message)

        response = client.chat.completions.create(
            model="qwen2.5-vl-72b-instruct",
            messages=messages,
        )

        content = response.choices[0].message.content

        content = clean_json(content)

        # used later for the messages
        content_str = content

        content = json.loads(content)

        processed_content = []

        for operation in content:
            if operation.get("operation") == "click":
                text_to_click = operation.get("text")
                if config.verbose:
                    print(
                        "[call_qwen_vl_with_ocr][click] text_to_click",
                        text_to_click,
                    )
                # Initialize EasyOCR Reader
                reader = easyocr.Reader(["en"])

                # Read the screenshot
                result = reader.readtext(screenshot_filename)

                text_element_index = get_text_element(
                    result, text_to_click, screenshot_filename
                )
                coordinates = get_text_coordinates(
                    result, text_element_index, screenshot_filename
                )

                # add `coordinates`` to `content`
                operation["x"] = coordinates["x"]
                operation["y"] = coordinates["y"]

                if config.verbose:
                    print(
                        "[call_qwen_vl_with_ocr][click] text_element_index",
                        text_element_index,
                    )
                    print(
                        "[call_qwen_vl_with_ocr][click] coordinates",
                        coordinates,
                    )
                    print(
                        "[call_qwen_vl_with_ocr][click] final operation",
                        operation,
                    )
                processed_content.append(operation)

            else:
                processed_content.append(operation)

        # wait to append the assistant message so that if the `processed_content` step fails we don't append a message and mess up message history
        assistant_message = {"role": "assistant", "content": content_str}
        messages.append(assistant_message)

        return processed_content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
        return gpt_4_fallback(messages, objective, model)


def call_gemini_pro_vision(messages, objective):
    """
    Get the next action for Self-Operating Computer using Gemini Pro Vision
    """
    if config.verbose:
        print(
            "[Self Operating Computer][call_gemini_pro_vision]",
        )
    # sleep for a second
    time.sleep(1)
    try:
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)
        # sleep for a second
        time.sleep(1)
        prompt = get_system_prompt("gemini-pro-vision", objective)

        model = config.initialize_google()
        if config.verbose:
            print("[call_gemini_pro_vision] model", model)

        response = model.generate_content([prompt, Image.open(screenshot_filename)])

        content = response.text[1:]
        if config.verbose:
            print("[call_gemini_pro_vision] response", response)
            print("[call_gemini_pro_vision] content", content)

        content = json.loads(content)
        if config.verbose:
            print(
                "[get_next_action][call_gemini_pro_vision] content",
                content,
            )

        return content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[Operate] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
        return call_gpt_4o(messages)


async def call_gpt_4o_with_ocr(messages, objective, model):
    if config.verbose:
        print("[call_gpt_4o_with_ocr]")

    # Construct the path to the file within the package
    try:
        time.sleep(1)
        client = config.initialize_openai()

        confirm_system_prompt(messages, objective, model)
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        with open(screenshot_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        vision_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
                },
            ],
        }
        messages.append(vision_message)

        response = client.chat.completions.create(
            model="o1",
            messages=messages,
        )

        content = response.choices[0].message.content

        content = clean_json(content)

        # used later for the messages
        content_str = content

        content = json.loads(content)

        processed_content = []

        for operation in content:
            if operation.get("operation") == "click":
                text_to_click = operation.get("text")
                if config.verbose:
                    print(
                        "[call_gpt_4o_with_ocr][click] text_to_click",
                        text_to_click,
                    )
                # Initialize EasyOCR Reader
                reader = easyocr.Reader(["en"])

                # Read the screenshot
                result = reader.readtext(screenshot_filename)

                text_element_index = get_text_element(
                    result, text_to_click, screenshot_filename
                )
                coordinates = get_text_coordinates(
                    result, text_element_index, screenshot_filename
                )

                # add `coordinates`` to `content`
                operation["x"] = coordinates["x"]
                operation["y"] = coordinates["y"]

                if config.verbose:
                    print(
                        "[call_gpt_4o_with_ocr][click] text_element_index",
                        text_element_index,
                    )
                    print(
                        "[call_gpt_4o_with_ocr][click] coordinates",
                        coordinates,
                    )
                    print(
                        "[call_gpt_4o_with_ocr][click] final operation",
                        operation,
                    )
                processed_content.append(operation)

            else:
                processed_content.append(operation)

        # wait to append the assistant message so that if the `processed_content` step fails we don't append a message and mess up message history
        assistant_message = {"role": "assistant", "content": content_str}
        messages.append(assistant_message)

        return processed_content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
        return gpt_4_fallback(messages, objective, model)


async def call_o1_with_ocr(messages, objective, model):
    if config.verbose:
        print("[call_o1_with_ocr]")

    # Construct the path to the file within the package
    try:
        time.sleep(1)
        client = config.initialize_openai()

        confirm_system_prompt(messages, objective, model)
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        with open(screenshot_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        vision_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"},
                },
            ],
        }
        messages.append(vision_message)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
        )

        content = response.choices[0].message.content

        content = clean_json(content)

        # used later for the messages
        content_str = content

        content = json.loads(content)

        processed_content = []

        for operation in content:
            if operation.get("operation") == "click":
                text_to_click = operation.get("text")
                if config.verbose:
                    print(
                        "[call_o1_with_ocr][click] text_to_click",
                        text_to_click,
                    )
                # Initialize EasyOCR Reader
                reader = easyocr.Reader(["en"])

                # Read the screenshot
                result = reader.readtext(screenshot_filename)

                text_element_index = get_text_element(
                    result, text_to_click, screenshot_filename
                )
                coordinates = get_text_coordinates(
                    result, text_element_index, screenshot_filename
                )

                # add `coordinates`` to `content`
                operation["x"] = coordinates["x"]
                operation["y"] = coordinates["y"]

                if config.verbose:
                    print(
                        "[call_o1_with_ocr][click] text_element_index",
                        text_element_index,
                    )
                    print(
                        "[call_o1_with_ocr][click] coordinates",
                        coordinates,
                    )
                    print(
                        "[call_o1_with_ocr][click] final operation",
                        operation,
                    )
                processed_content.append(operation)

            else:
                processed_content.append(operation)

        # wait to append the assistant message so that if the `processed_content` step fails we don't append a message and mess up message history
        assistant_message = {"role": "assistant", "content": content_str}
        messages.append(assistant_message)

        return processed_content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
        return gpt_4_fallback(messages, objective, model)


async def call_gpt_4o_labeled(messages, objective, model):
    time.sleep(1)

    try:
        client = config.initialize_openai()

        confirm_system_prompt(messages, objective, model)
        file_path = pkg_resources.resource_filename("operate.models.weights", "best.pt")
        yolo_model = YOLO(file_path)  # Load your trained model
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        with open(screenshot_filename, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

        img_base64_labeled, label_coordinates = add_labels(img_base64, yolo_model)

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        if config.verbose:
            print(
                "[call_gpt_4_vision_preview_labeled] user_prompt",
                user_prompt,
            )

        vision_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{img_base64_labeled}"
                    },
                },
            ],
        }
        messages.append(vision_message)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            presence_penalty=1,
            frequency_penalty=1,
        )

        content = response.choices[0].message.content

        content = clean_json(content)

        assistant_message = {"role": "assistant", "content": content}

        messages.append(assistant_message)

        content = json.loads(content)
        if config.verbose:
            print(
                "[call_gpt_4_vision_preview_labeled] content",
                content,
            )

        processed_content = []

        for operation in content:
            print(
                "[call_gpt_4_vision_preview_labeled] for operation in content",
                operation,
            )
            if operation.get("operation") == "click":
                label = operation.get("label")
                if config.verbose:
                    print(
                        "[Self Operating Computer][call_gpt_4_vision_preview_labeled] label",
                        label,
                    )

                coordinates = get_label_coordinates(label, label_coordinates)
                if config.verbose:
                    print(
                        "[Self Operating Computer][call_gpt_4_vision_preview_labeled] coordinates",
                        coordinates,
                    )
                image = Image.open(
                    io.BytesIO(base64.b64decode(img_base64))
                )  # Load the image to get its size
                image_size = image.size  # Get the size of the image (width, height)
                click_position_percent = get_click_position_in_percent(
                    coordinates, image_size
                )
                if config.verbose:
                    print(
                        "[Self Operating Computer][call_gpt_4_vision_preview_labeled] click_position_percent",
                        click_position_percent,
                    )
                if not click_position_percent:
                    print(
                        f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Error] Failed to get click position in percent. Trying another method {ANSI_RESET}"
                    )
                    return call_gpt_4o(messages)

                x_percent = f"{click_position_percent[0]:.2f}"
                y_percent = f"{click_position_percent[1]:.2f}"
                operation["x"] = x_percent
                operation["y"] = y_percent
                if config.verbose:
                    print(
                        "[Self Operating Computer][call_gpt_4_vision_preview_labeled] new click operation",
                        operation,
                    )
                processed_content.append(operation)
            else:
                if config.verbose:
                    print(
                        "[Self Operating Computer][call_gpt_4_vision_preview_labeled] .append none click operation",
                        operation,
                    )

                processed_content.append(operation)

            if config.verbose:
                print(
                    "[Self Operating Computer][call_gpt_4_vision_preview_labeled] new processed_content",
                    processed_content,
                )
            return processed_content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
        return call_gpt_4o(messages)


def call_ollama_llava(messages):
    if config.verbose:
        print("[call_ollama_llava]")
    time.sleep(1)
    try:
        model = config.initialize_ollama()
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        # Call the function to capture the screen with the cursor
        capture_screen_with_cursor(screenshot_filename)

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        if config.verbose:
            print(
                "[call_ollama_llava] user_prompt",
                user_prompt,
            )

        vision_message = {
            "role": "user",
            "content": user_prompt,
            "images": [screenshot_filename],
        }
        messages.append(vision_message)

        response = model.chat(
            model="llava",
            messages=messages,
        )

        # Important: Remove the image path from the message history.
        # Ollama will attempt to load each image reference and will
        # eventually timeout.
        messages[-1]["images"] = None

        content = response["message"]["content"].strip()

        content = clean_json(content)

        assistant_message = {"role": "assistant", "content": content}
        if config.verbose:
            print(
                "[call_ollama_llava] content",
                content,
            )
        content = json.loads(content)

        messages.append(assistant_message)

        return content

    except ollama.ResponseError as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Operate] Couldn't connect to Ollama. With Ollama installed, run `ollama pull llava` then `ollama serve`{ANSI_RESET}",
            e,
        )

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[llava] That did not work. Trying again {ANSI_RESET}",
            e,
        )
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Error] AI response was {ANSI_RESET}",
            content,
        )
        if config.verbose:
            traceback.print_exc()
        return call_ollama_llava(messages)


async def call_claude_3_with_ocr(messages, objective, model):
    if config.verbose:
        print("[call_claude_3_with_ocr]")

    try:
        time.sleep(1)
        client = config.initialize_anthropic()

        confirm_system_prompt(messages, objective, model)
        screenshots_dir = "screenshots"
        if not os.path.exists(screenshots_dir):
            os.makedirs(screenshots_dir)

        screenshot_filename = os.path.join(screenshots_dir, "screenshot.png")
        capture_screen_with_cursor(screenshot_filename)

        # downsize screenshot due to 5MB size limit
        with open(screenshot_filename, "rb") as img_file:
            img = Image.open(img_file)

            # Convert RGBA to RGB
            if img.mode == "RGBA":
                img = img.convert("RGB")

            # Calculate the new dimensions while maintaining the aspect ratio
            original_width, original_height = img.size
            aspect_ratio = original_width / original_height
            new_width = 2560  # Adjust this value to achieve the desired file size
            new_height = int(new_width / aspect_ratio)
            if config.verbose:
                print("[call_claude_3_with_ocr] resizing claude")

            # Resize the image
            img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Save the resized and converted image to a BytesIO object for JPEG format
            img_buffer = io.BytesIO()
            img_resized.save(
                img_buffer, format="JPEG", quality=85
            )  # Adjust the quality parameter as needed
            img_buffer.seek(0)

            # Encode the resized image as base64
            img_data = base64.b64encode(img_buffer.getvalue()).decode("utf-8")

        if len(messages) == 1:
            user_prompt = get_user_first_message_prompt()
        else:
            user_prompt = get_user_prompt()

        vision_message = {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_data,
                    },
                },
                {
                    "type": "text",
                    "text": user_prompt
                    + "**REMEMBER** Only output json format, do not append any other text.",
                },
            ],
        }
        messages.append(vision_message)

        # anthropic api expect system prompt as an separate argument
        response = client.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=3000,
            system=messages[0]["content"],
            messages=messages[1:],
        )

        content = response.content[0].text
        content = clean_json(content)
        content_str = content
        try:
            content = json.loads(content)
        # rework for json mode output
        except json.JSONDecodeError as e:
            if config.verbose:
                print(
                    f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_RED}[Error] JSONDecodeError: {e} {ANSI_RESET}"
                )
            response = client.messages.create(
                model="claude-3-opus-20240229",
                max_tokens=3000,
                system=f"This json string is not valid, when using with json.loads(content) \
                it throws the following error: {e}, return correct json string. \
                **REMEMBER** Only output json format, do not append any other text.",
                messages=[{"role": "user", "content": content}],
            )
            content = response.content[0].text
            content = clean_json(content)
            content_str = content
            content = json.loads(content)

        if config.verbose:
            print(
                f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] content: {content} {ANSI_RESET}"
            )
        processed_content = []

        for operation in content:
            if operation.get("operation") == "click":
                text_to_click = operation.get("text")
                if config.verbose:
                    print(
                        "[call_claude_3_ocr][click] text_to_click",
                        text_to_click,
                    )
                # Initialize EasyOCR Reader
                reader = easyocr.Reader(["en"])

                # Read the screenshot
                result = reader.readtext(screenshot_filename)

                # limit the text to extract has a higher success rate
                text_element_index = get_text_element(
                    result, text_to_click[:3], screenshot_filename
                )
                coordinates = get_text_coordinates(
                    result, text_element_index, screenshot_filename
                )

                # add `coordinates`` to `content`
                operation["x"] = coordinates["x"]
                operation["y"] = coordinates["y"]

                if config.verbose:
                    print(
                        "[call_claude_3_ocr][click] text_element_index",
                        text_element_index,
                    )
                    print(
                        "[call_claude_3_ocr][click] coordinates",
                        coordinates,
                    )
                    print(
                        "[call_claude_3_ocr][click] final operation",
                        operation,
                    )
                processed_content.append(operation)

            else:
                processed_content.append(operation)

        assistant_message = {"role": "assistant", "content": content_str}
        messages.append(assistant_message)

        return processed_content

    except Exception as e:
        print(
            f"{ANSI_GREEN}[Self-Operating Computer]{ANSI_BRIGHT_MAGENTA}[{model}] That did not work. Trying another method {ANSI_RESET}"
        )
        if config.verbose:
            print("[Self-Operating Computer][Operate] error", e)
            traceback.print_exc()
            print("message before convertion ", messages)

        # Convert the messages to the GPT-4 format
        gpt4_messages = [messages[0]]  # Include the system message
        for message in messages[1:]:
            if message["role"] == "user":
                # Update the image type format from "source" to "url"
                updated_content = []
                for item in message["content"]:
                    if isinstance(item, dict) and "type" in item:
                        if item["type"] == "image":
                            updated_content.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{item['source']['data']}"
                                    },
                                }
                            )
                        else:
                            updated_content.append(item)

                gpt4_messages.append({"role": "user", "content": updated_content})
            elif message["role"] == "assistant":
                gpt4_messages.append(
                    {"role": "assistant", "content": message["content"]}
                )

        return gpt_4_fallback(gpt4_messages, objective, model)


def get_last_assistant_message(messages):
    """
    Retrieve the last message from the assistant in the messages array.
    If the last assistant message is the first message in the array, return None.
    """
    for index in reversed(range(len(messages))):
        if messages[index]["role"] == "assistant":
            if index == 0:  # Check if the assistant message is the first in the array
                return None
            else:
                return messages[index]
    return None  # Return None if no assistant message is found


def gpt_4_fallback(messages, objective, model):
    if config.verbose:
        print("[gpt_4_fallback]")
    system_prompt = get_system_prompt("gpt-4o", objective)
    new_system_message = {"role": "system", "content": system_prompt}
    # remove and replace the first message in `messages` with `new_system_message`

    messages[0] = new_system_message

    if config.verbose:
        print("[gpt_4_fallback][updated]")
        print("[gpt_4_fallback][updated] len(messages)", len(messages))

    return call_gpt_4o(messages)


def confirm_system_prompt(messages, objective, model):
    """
    On `Exception` we default to `call_gpt_4_vision_preview` so we have this function to reassign system prompt in case of a previous failure
    """
    if config.verbose:
        print("[confirm_system_prompt] model", model)

    system_prompt = get_system_prompt(model, objective)
    new_system_message = {"role": "system", "content": system_prompt}
    # remove and replace the first message in `messages` with `new_system_message`

    messages[0] = new_system_message

    if config.verbose:
        print("[confirm_system_prompt]")
        print("[confirm_system_prompt] len(messages)", len(messages))
        for m in messages:
            if m["role"] != "user":
                print("--------------------[message]--------------------")
                print("[confirm_system_prompt][message] role", m["role"])
                print("[confirm_system_prompt][message] content", m["content"])
                print("------------------[end message]------------------")


def clean_json(content):
    if config.verbose:
        print("\n\n[clean_json] content before cleaning", content)
    if content.startswith("```json"):
        content = content[
            len("```json") :
        ].strip()  # Remove starting ```json and trim whitespace
    elif content.startswith("```"):
        content = content[
            len("```") :
        ].strip()  # Remove starting ``` and trim whitespace
    if content.endswith("```"):
        content = content[
            : -len("```")
        ].strip()  # Remove ending ``` and trim whitespace

    # Normalize line breaks and remove any unwanted characters
    content = "\n".join(line.strip() for line in content.splitlines())

    if config.verbose:
        print("\n\n[clean_json] content after cleaning", content)

    return content
