from enum import Enum

class CB(str, Enum):
	# Global menu
	GM_ADD_CHANNEL = "gm_adding_channels"
	GM_MY_CHANNELS = "gm_my_channels"
	GM_MY_DATABASE = "gm_my_database"
	GM_CREATE_POST = "gm_create_post"
	GM_TIME_POST = "gm_time_post_world"
	GM_GLOBAL_MENU = "gm_global_menu"

	# Content plan
	CP_OPEN = "cp_open"
	CP_PICK_CHANNEL = "cp_pick_channel_"  # +id
	CP_DAY_PREV = "cp_day_prev"
	CP_DAY_NEXT = "cp_day_next"
	CP_REPEAT_OFF = "cp_repeat_off"  # отключить автоповтор для серии

	# Posting
	POST_SEND = "post_send"
	POST_NEXT = "post_next"
	POST_SCHEDULE = "post_schedule"
	POST_BACK = "post_back"
	POST_EDIT_TEXT = "post_edit_text"
	POST_ADD_PHOTO = "post_add_photo"
	POST_ADD_VIDEO = "post_add_video"
	POST_ADD_ANIMATION = "post_add_animation"
	POST_ADD_AUDIO = "post_add_audio"
	POST_ADD_BUTTON = "post_add_button"
	POST_DELETE_BUTTONS = "post_delete_buttons"
	# Prefix for removing a single button: post_btn_rm:{row}:{col}
	POST_BUTTON_REMOVE_PREFIX = "post_btn_rm:"
	POST_ADD_MEDIA = "post_add_media"
	POST_MEDIA_BACK = "post_media_back"

	# Preview (link preview for text posts)
	PREVIEW_MENU = "preview_menu"
	PREVIEW_TOGGLE = "preview_toggle"
	PREVIEW_POS_TOGGLE = "preview_pos_toggle"
	PREVIEW_CLEAR = "preview_clear"

	# Media settings (для опубликованных/редактор)
	MEDIA_MENU = "media_menu"
	MEDIA_POS_TOGGLE = "media_pos_toggle"
	MEDIA_SPOILER_TOGGLE = "media_spoiler_toggle"
	MEDIA_REPLACE = "media_replace"
	MEDIA_PAID_TOGGLE = "media_paid_toggle"
	MEDIA_PAID_PRICE = "media_paid_price"
	POST_TOGGLE_NOTIFY = "post_toggle_notify"
	POST_TOGGLE_AUTOSIGN = "post_toggle_autosign"
	POST_TOGGLE_PIN = "post_toggle_pin"
	POST_TOGGLE_COMMENTS = "post_toggle_comments"
	POST_PICK_CH_PREFIX = "post_pick_ch_"  # +id

	# New Create Post card / Ad & AI quick actions
	POST_TOGGLE_AD = "post_toggle_ad"
	POST_AD_OPEN = "post_ad_open"
	POST_AD_NEW = "post_ad_new"
	POST_AD_EDIT = "post_ad_edit"
	POST_AD_PRESET_PREFIX = "post_ad_preset_"  # +h_top_autodelHours, e.g., 1_24
	POST_AI_QUICK = "post_ai_quick"
	AI_BACK_TO_CREATE = "ai_back_to_create"
	AI_QUICK_APPLY = "ai_quick_apply"
	POST_AD_SETTINGS_OPEN = "post_ad_settings_open"

	# Posting settings menu
	POST_SETTINGS_TIMER = "post_set_timer"
	POST_SETTINGS_REPEAT = "post_set_repeat"
	CP_TOGGLE_REPEATS = "cp_toggle_repeats"
	# Repeat submenu
	POST_REPEAT_PRESET_PREFIX = "post_repeat_preset_"  # +seconds
	POST_REPEAT_CLEAR = "post_repeat_clear"
	POST_SETTINGS_FORWARD = "post_set_forward"
	POST_SETTINGS_DEFER = "post_set_defer"
	POST_SETTINGS_BACK = "post_settings_back"
	POST_SETTINGS_PUBLISH = "post_settings_publish"
	# Timer submenu
	POST_TIMER_FREE = "post_timer_free"
	POST_TIMER_CLEAR = "post_timer_clear"
	POST_TIMER_PRESET_PREFIX = "post_timer_preset_"  # +seconds
	POST_TIMER_REPORT = "post_timer_report"
	# Views-based deletion submenu
	POST_VIEW_MENU = "post_view_menu"
	POST_VIEW_PRESET_PREFIX = "post_view_preset_"  # +views
	POST_VIEW_CLEAR = "post_view_clear"
	POST_VIEW_REPORT = "post_view_report"

	# Ad/top-time
	POST_TOP_OPEN = "post_top_open"
	POST_TOP_SET_PREFIX = "post_top_set_"  # +seconds
	POST_TOP_BACK = "post_top_back"
	# Forward selection
	POST_FWD_TOGGLE_PREFIX = "post_fwd_tgl_"  # +channel_id
	POST_FWD_ALL = "post_fwd_all"
	POST_FWD_NONE = "post_fwd_none"
	# Replace autosign in posted messages
	POST_REPLACE_AUTOSIGN = "post_replace_autosign"
	# Edit menu
	EDIT_DUP = "edit_dup"
	EDIT_MODIFY = "edit_modify"
	EDIT_AD_TOGGLE = "edit_ad_toggle"
	EDIT_AUTODEL = "edit_autodel"
	EDIT_DELETE = "edit_delete"
	SET_CH_TOGGLE_DELETE_SERVICE = "set_ch_toggle_delete_service_"  # +id
	OPEN_SERVICE_MENU = "open_service_menu_"  # +type_channel_or_chat:cid
	SERVICE_TOGGLE_PREFIX = "service_toggle_"  # +type:key:cid

	# NeuroPost (in posting editor)
	POST_AI = "post_ai"
	POST_NEURO = "post_neuro"
	POST_NEURO_TOPIC = "post_neuro_topic"
	POST_NEURO_LINK = "post_neuro_link"
	POST_NEURO_MEDIA = "post_neuro_media"
	POST_NEURO_TOGGLE_AB = "post_neuro_toggle_ab"

	# Channel settings categories
	SETTINGS_POST = "settings_post"
	SETTINGS_MEDIA = "settings_media"
	SETTINGS_BUTTON = "settings_button"
	SETTINGS_BACK = "settings_back" 