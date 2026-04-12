import json
import base64

def generate_camstar_auth_token(username, password):
    """
    通过用户名和密码生成 Camstar 的 Auth Token。
    原理：构造特定的凭证 JSON 然后进行 Base64 编码。
    """
    # 按照前端代码的格式构造字典
    credentials = {
        "username": "CamstarAdmin",
        "Password": {
            "value": "Cam1star",
            "isEncrypted": False
        }
    }
    
    # 将字典转换为 JSON 字符串
    # separators=(',', ':') 确保和标准前端字符串化后不带多余空格
    json_creds = json.dumps(credentials, separators=(',', ':'))
    
    # 进行 Base64 编码
    token_bytes = base64.b64encode(json_creds.encode('utf-8'))
    token = token_bytes.decode('utf-8')
    
    return token

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Generate Camstar Auth Token")
    parser.add_argument("--username", "-u", type=str, default="CamstarAdmin", help="The username for login")
    parser.add_argument("--password", "-p", type=str, required=True, help="The password for login")
    
    args = parser.parse_args()
    
    try:
        auth_token = generate_camstar_auth_token(args.username, args.password)
        print("====== 生成 CAMSTAR_AUTH_TOKEN ======")
        print(auth_token)
        print("=====================================")
    except Exception as e:
        print(f"Error generating token: {e}", file=sys.stderr)
