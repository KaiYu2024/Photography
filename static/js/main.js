document.getElementById('uploadForm').addEventListener('submit', async function(event) {
    event.preventDefault();

    const imageInput = document.getElementById('imageInput');
    const imageFile = imageInput.files[0];

    if (!imageFile) {
        alert('請先選擇一張圖片！');
        return;
    }

    const formData = new FormData();
    formData.append('image', imageFile);

    const loadingSection = document.getElementById('loading');
    const resultsSection = document.getElementById('results');
    const textResult = document.getElementById('textResult');
    const boxedImage = document.getElementById('boxedImage');
    const adjustedImage = document.getElementById('adjustedImage');

    // 隱藏結果區，顯示載入區
    loadingSection.style.display = 'flex';

    try {
        const response = await fetch('/analyze_image', {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            throw new Error('伺服器回傳錯誤');
        }
        loadingSection.style.display = 'none';

        const data = await response.json();
        
        // 顯示結果
        textResult.textContent = data.text;
        boxedImage.src = data.boxedImageUrl;
        adjustedImage.src = data.adjustedImageUrl;

        loadingSection.classList.add('hidden');
        resultsSection.classList.remove('hidden');

    } catch (error) {
        loadingSection.classList.add('hidden');
        alert('分析失敗：' + error.message);
        console.error('Error:', error);
    }
});