from main_whatsapp import reload_config, slack_to_whatsapp_map
reload_config()
print("S2W MAP:")
for k, v in slack_to_whatsapp_map.items():
    print(f"{k}: {v}")
