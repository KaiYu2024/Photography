
import os
import tempfile
import sys
import configparser
import time

from flask import Flask, request, abort, send_from_directory, request, jsonify, render_template, url_for
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage
)
from io import BytesIO
from azure.ai.vision.imageanalysis import ImageAnalysisClient
from azure.ai.vision.imageanalysis.models import VisualFeatures
from azure.core.credentials import AzureKeyCredential
from PIL import Image, ImageEnhance, ImageDraw

from openai import AzureOpenAI
from azure.storage.blob import BlobServiceClient, ContentSettings

# Config Parser
config = configparser.ConfigParser()
config.read("config.ini")

# ImageAnalysis Setup
image_client = ImageAnalysisClient(
    credential=AzureKeyCredential(config["AzureVision"]["Key"]),
    endpoint=config["AzureVision"]["EndPoint"],
    region=config["AzureVision"]["Region"],
)
# OpenAI Setup
azure_client = AzureOpenAI(
    azure_endpoint=os.getenv("OPENAI_API_ENDPOINT"),
    api_key=os.getenv("OPENAI_API_KEY"),
    api_version=os.getenv("OPENAI_API_VERSION")
)
URL = config["Deploy"]["WEBSITE"]
# 本地暫存圖片資料夾
UPLOAD_FOLDER="static"

#app = Flask(__name__)
# 指定靜態資料夾，並設定一個特殊的 URL 前綴，例如 '/files'
app = Flask(__name__, static_folder="static", static_url_path="/files")

# Channel Access Token & Secret
channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
channel_secret = os.getenv('CHANNEL_SECRET')
if channel_secret is None:
    print('Specify LINE_CHANNEL_SECRET as environment variable.')
    sys.exit(1)
if channel_access_token is None:
    print('Specify LINE_CHANNEL_ACCESS_TOKEN as environment variable.')
    sys.exit(1)

handler = WebhookHandler(channel_secret)

configuration = Configuration(
    access_token=channel_access_token
)

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # parse webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/files/<path:filename>")
def serve_static(filename):
    """
    自定義靜態檔案路由，並禁用快取。
    """
    response = send_from_directory(app.static_folder, filename)
    # 禁用快取，確保每次都重新載入
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@handler.add(MessageEvent, message=TextMessageContent)
def message_text(event):
    """
    user input text message
    """    
    with ApiClient(configuration) as api_client:
        line_bot_api=MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='請傳送圖片進行分析')],
            )
        )

# 圖片訊息
@handler.add(MessageEvent, message=ImageMessageContent)
def message_image(event):
    """
    user input image message
    """ 
    reply_messages = []
    
    with ApiClient(configuration) as api_client: 
        line_bot_blob_api=MessagingApiBlob(api_client)
        message_content=line_bot_blob_api.get_message_content(message_id=event.message.id)

    # # 獲取當前的時間
    # timestamp = int(time.time())

    # # 定義圖片路徑，加上時間標籤
    # original_path = f'{UPLOAD_FOLDER}/original_image_{timestamp}.jpg'
    # adjusted_path = f'{UPLOAD_FOLDER}/adjusted_image_{timestamp}.jpg'
    # boxed_path = f'{UPLOAD_FOLDER}/image_with_box_{timestamp}.jpg'

    # 保存原始圖片 
    image = Image.open(BytesIO(message_content))    
    original_image_url = upload_image_to_azure(image)
    try:
        # 圖片分析與處理
        analyze_result,adjusted_image_url,image_with_box_url = fnAnalysis(image,original_image_url)

        # OpenAI 評價
        reply_messages.append(
            TextMessage(text=analyze_result)
        )
        # 文字:建議裁切範圍
        reply_messages.append(
            TextMessage(text='建議裁切範圍如下')
        )
        # 裁切建議圖(紅框)
        reply_messages.append(
            ImageMessage(original_content_url=image_with_box_url,  # 放大顯示圖
                        preview_image_url=image_with_box_url)   # 預覽圖(縮圖)
        )
        # 文字:調整後的圖片
        reply_messages.append(
            TextMessage(text='調整後的圖片如下')
        )
        # 修正後的圖
        reply_messages.append(
            ImageMessage(original_content_url=adjusted_image_url,  # 放大顯示圖
                        preview_image_url=adjusted_image_url)     # 預覽圖(縮圖)
        )
        print(reply_messages)
        # 回覆訊息
        with ApiClient(configuration) as api_client:
            line_bot_api=MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=reply_messages,
            ))
    except Exception as e:
        print(f'錯誤:{e}')
    finally:
        # 刪除 Azure Blob Storage 中的圖片
        delete_blob_image(original_image_url)

# 接收圖片
def fnAnalysis(image:bytes,original_image_url:str) -> str:
    '''Analyze Image by azure ai vision
    image - the user inputs the image, save image to BytesIO
    original_image_url - the path of original image 
    adjusted_image_url - the path of adjusted image 
    image_with_box_url - the path of image with box
    return str for Open ai response message
    '''
    
    # 進行圖片分析
    analysis_result = analyze_image_with_azure(image)

    # 根據 AI 建議進行圖片調整
    adjusted_image_url = process_and_adjust_image(analysis_result, image)

    # 標註建議調整的部分
    image_with_box_url = draw_smart_crop_box(analysis_result, image)
    
    # 輸出文字評價
    msg = openai_gpt4v_sdk(analysis_result, original_image_url)
    #msg = '圖片評價與建議'

    time.sleep(3)

    return msg,adjusted_image_url,image_with_box_url

# 使用 Azure Vision API 分析圖片
def analyze_image_with_azure(image)-> dict:
    '''analyze image by azure ai vision
    return analysis result including CAPTION, TAGS and SMART_CROPS 
    '''
    print("--- 正在分析圖片中 ---")
    image_bytes = BytesIO()
    # 暫存圖片到 BytesIO 物件
    image.save(image_bytes,format='JPEG') #JPGE
    image_bytes.seek(0)

    # 判斷原始圖片是直向還是橫向
    width, height = image.size
    aspect_ratio = width / height
    
    if aspect_ratio > 1.0: # 橫向照片
        crop_ratios = [1.77, 1.33, 1.0, 0.75]
    else: # 直向照片 (或方形)
        crop_ratios = [0.75, 1.0, 1.33, 1.77]

    # 執行分析，要求標註、描述和智慧型裁切
    analysis_result = image_client.analyze(
        image_bytes,
        visual_features=[
            VisualFeatures.CAPTION, # 圖片的描述
            VisualFeatures.TAGS,    # 圖片中的標籤
            VisualFeatures.SMART_CROPS, # 智慧裁切建議
        ],
        smart_crops_aspect_ratios=crop_ratios
    )
    return analysis_result

# 根據分析結果進行影像後製和輸出
def process_and_adjust_image(analysis_result, original_image_data):
    '''crop and enhace image
    analysis_result - from def analyze_image_with_azure
    original_image_data - from the user
    return adjusted_image_url
    '''
    print("\n--- 根據分析結果進行影像後製 ---")
    # 獲取智慧型裁切的建議
    smart_crop_region = None
    if analysis_result.smart_crops and len(analysis_result.smart_crops) > 0:
        smart_crop_region = analysis_result.smart_crops.list[0]
        x = smart_crop_region.bounding_box.x
        y = smart_crop_region.bounding_box.y
        width = smart_crop_region.bounding_box.width
        height = smart_crop_region.bounding_box.height

        print(f"AI 建議的智慧型裁切區域: 座標 ({x}, {y}), 寬 {width}, 高 {height}")

        # 進行裁切
        cropped_img = original_image_data.crop((x, y, x + width, y + height))
        
    else:
        print("沒有找到適合的智慧型裁切建議，使用原始圖片進行調整。")
        cropped_img = original_image_data

    # 模擬專業攝影師的光線和色彩調整
    print("調整對比度、亮度和色彩飽和度...")

    # 調整對比度 (Contrast)
    enhancer_contrast = ImageEnhance.Contrast(cropped_img)
    adjusted_img = enhancer_contrast.enhance(1.2) # 增加 20% 對比

    # 調整亮度 (Brightness)
    enhancer_brightness = ImageEnhance.Brightness(adjusted_img)
    adjusted_img = enhancer_brightness.enhance(1.05) # 增加 5% 亮度

    # 調整飽和度 (Color Saturation)
    enhancer_color = ImageEnhance.Color(adjusted_img)
    adjusted_img = enhancer_color.enhance(1.2) # 增加 20% 飽和度

    # 保存調整後的圖片
    adjusted_image_url = upload_image_to_azure(adjusted_img)

    return adjusted_image_url
    
# 在圖片上標註 AI 建議的區域
def draw_smart_crop_box(analysis_result, original_image_data):
    '''draw a red box to mark crop area on the image
    analysis_result - from def analyze_image_with_azure
    original_image_data - from the user
    return image_with_box_url
    '''
    print("\n--- 在圖片上標註 AI 建議的區域 ---")
    draw = ImageDraw.Draw(original_image_data)

    if analysis_result.smart_crops and len(analysis_result.smart_crops) > 0:
        smart_crop_region = analysis_result.smart_crops.list[0]
        box = (
            smart_crop_region.bounding_box.x,
            smart_crop_region.bounding_box.y,
            smart_crop_region.bounding_box.x + smart_crop_region.bounding_box.width,
            smart_crop_region.bounding_box.y + smart_crop_region.bounding_box.height
        )
        # 用紅色邊框標示
        draw.rectangle(box, outline="red", width=5)
        image_with_box_url = upload_image_to_azure(original_image_data)

    return image_with_box_url

# 使用 OpenAI GPT-4V SDK 進行圖片評價
def openai_gpt4v_sdk(analysis_result, user_image_url:str)->str:
    '''ask open ai
    analysis_result - from def analyze_image_with_azure
    user_image_url - the path of original image
    '''
    print("--- 正在進行圖片評價 ---")
    tag_names = [tag.name for tag in analysis_result.tags.list[:5]]  # 取前5個標籤
    photo_tags =  f'這張照片偵測到的主體有{'、'.join(tag_names)}。' if len(tag_names) > 0 else ''
    prompt_text = f"""
        你是一位經驗豐富的手機攝影專家，請根據我提供的照片，給予具體的攝影教學。     
        {photo_tags}
        請以簡潔、專業且易懂的語氣，從以下三個面向進行分析和建議：

        1.  構圖與焦段 (Composition & Focal Length)：
            * 這張照片構圖表現如何？會建議運用什麼構圖法嗎? 例如：三分法、對角線構圖、框架構圖或黃金分割等等。
            * 以手機攝影的角度來看，這張照片的焦段選擇是否合適？如果想拍出更好的效果，建議使用廣角、標準或長焦鏡頭？

        2.  拍攝角度 (Shooting Angle)：
            * 目前的拍攝角度（平視、仰視、俯視）有何優缺點？
            * 如果要突出主體或創造不同氛圍，建議從哪個角度重新拍攝？例如，是否應該蹲低、尋找高處、往前走幾步，或從側面拍攝？

        3.  光線與曝光 (Lighting & Exposure)：
            * 這張照片的光線來源是順光、逆光還是側光？這種光線對畫面產生了什麼影響？
            * 照片的亮度與曝光是否適當？如果過亮或過暗，建議如何在手機上調整曝光補償（EV 值）？

        請按照這個結構，提供具體、可執行的建議，讓一個手機攝影初學者也能輕鬆理解並應用。
        請用繁體中文，並以條列式呈現，並在每個大標題前面放上相關的顏文字，語氣專業且友善，整段文字不超過一百五十個字。
    """
    try:
        response = azure_client.chat.completions.create(
            model=os.getenv("GPT4V_DEPLOYMENT_NAME"),
            messages=[
                {'role': 'system','content': prompt_text},
                {"role": "user","content": 
                    [
                        {
                            "type": "image_url",
                            "image_url": {"url": user_image_url},
                        },
                    ],
                },
            ],
            max_tokens=800,
            top_p=0.95
        )
        print('已完成評價')
        return response.choices[0].message.content
    except Exception as error:
        print("Error:", error)
        return f"系統異常，請再試一次。{error}"

# 上傳至 Azure Blob Storage
def upload_image_to_azure(image: Image.Image):
    """將 PIL Image 物件上傳到 Azure Blob Storage 並回傳 URL"""
    try:
        # 從環境變數讀取設定
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name = "image" # 替換成你剛剛建立的容器名稱
        
        if not connection_string:
            print("錯誤：Azure Storage 連接字串未設定。")
            return None

        # 建立 Blob 服務客戶端
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client(container_name)

        # 將圖片轉換為二進位資料
        image_bytes = BytesIO()
        image.save(image_bytes, format="JPEG")
        image_bytes.seek(0)

        # 建立一個獨一無二的檔名
        blob_name = f"image_{os.urandom(8).hex()}.jpg"
        
        # 上傳圖片
        blob_client = container_client.get_blob_client(blob=blob_name)
        blob_client.upload_blob(image_bytes, overwrite=True)

        # 回傳公開的 URL
        return blob_client.url
    except Exception as e:
        print(f"上傳圖片到 Azure Blob 失敗: {e}")
        return None

# 刪除 Azure Blob Storage 中的圖片
def delete_blob_image(image_url):
    """
    根據圖片的 URL 刪除 Azure Blob Storage 中的圖片。
    
    Args:
        image_url: 上傳後回傳的圖片公開 URL。
    """
    try:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if not connection_string:
            print("錯誤：Azure Storage 連接字串未設定。")
            return
            
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        
        # 從 URL 中解析出容器名稱和 Blob 名稱
        from urllib.parse import urlparse
        parsed_url = urlparse(image_url)
        container_name = parsed_url.path.split('/')[1]
        blob_name = '/'.join(parsed_url.path.split('/')[2:])
        
        container_client = blob_service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)
        
        # 刪除 Blob
        blob_client.delete_blob()
        print(f"成功刪除圖片：{image_url}")
        
    except Exception as e:
        print(f"刪除圖片時發生錯誤：{e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze_image", methods=['POST'])
def analyze_image_from_web():
    # 接收上傳的圖片
    image_file = request.files['image']
    if not image_file:
        return jsonify({"error": "請上傳圖片檔案"}), 400

    # 上傳原始圖片   
    image = Image.open(image_file.stream)
    original_image_url = upload_image_to_azure(image)
    adjusted_image_url = ''
    image_with_box_url = ''
    try:
        analyze_result,adjusted_image_url,image_with_box_url = fnAnalysis(image,original_image_url)
        # 組合回傳資料
        response_data = {
            "text": analyze_result,
            "adjustedImageUrl": adjusted_image_url,
            "boxedImageUrl": image_with_box_url,
        }
        
        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error processing image from web: {e}")
        return jsonify({"error": "圖片處理失敗"}), 500
    finally:
        # 刪除 Azure Blob Storage 中的圖片
        delete_blob_image(original_image_url)
        #delete_blob_image(adjusted_image_url)
        #delete_blob_image(image_with_box_url)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000)