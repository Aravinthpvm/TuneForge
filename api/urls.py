from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path('auth/state', views.auth_state_view, name='auth_state'),
    path('auth/setup', views.auth_setup_view, name='auth_setup'),
    path('auth/login', views.auth_login_view, name='auth_login'),
    path('auth/logout', views.auth_logout_view, name='auth_logout'),
    path('auth/password', views.auth_change_password_view, name='auth_password'),
    
    # Health & Settings
    path('health', views.health_view, name='health'),
    path('settings', views.settings_view, name='settings'),
    path('settings/save', views.settings_view, name='settings_save'),
    path('settings/apple-credentials', views.settings_apple_credentials_view, name='settings_apple_credentials'),
    path('settings/apple-credentials/login', views.settings_apple_credentials_login_post, name='settings_apple_credentials_login'),
    path('settings/apple-credentials/login-status', views.settings_apple_credentials_login_status_get, name='settings_apple_credentials_login_status'),
    path('settings/apple-credentials/2fa', views.settings_apple_credentials_2fa_post, name='settings_apple_credentials_2fa'),
    path('settings/apple-credentials/cancel-login', views.settings_apple_credentials_cancel_login_post, name='settings_apple_credentials_cancel_login'),
    path('settings/media-user-token', views.settings_media_user_token_view, name='settings_media_user_token'),
    
    # Search & Metadata
    path('search', views.search_view, name='search'),
    path('album/<str:id>', views.album_detail_view, name='album_detail'),
    path('artist/<str:id>', views.artist_detail_view, name='artist_detail'),
    path('playlist/library/<str:library_id>', views.playlist_library_detail_view, name='playlist_library_detail'),
    path('playlist/<str:id>', views.playlist_detail_view, name='playlist_detail'),
    
    # Downloads & Queue
    path('download', views.download_album_view, name='download'),
    path('download/album', views.download_album_view, name='download_album'),
    path('download/song', views.download_song_view, name='download_song'),
    path('download/playlist', views.download_playlist_view, name='download_playlist'),
    path('download/cancel-all', views.download_cancel_all_view, name='download_cancel_all'),
    path('download/<str:id>', views.download_cancel_path_view, name='download_cancel_path'),
    path('queue', views.queue_list_view, name='queue_list'),
    path('queue/cancel', views.queue_cancel_view, name='queue_cancel'),
    
    # Library Index
    path('library', views.library_index_view, name='library_index'),
    path('library/presence', views.library_presence_view, name='library_presence'),
    path('library/song', views.library_delete_song_view, name='library_delete_song'),
    path('library/playlist', views.library_delete_playlist_view, name='library_delete_playlist'),
    path('library/album', views.library_delete_album_view, name='library_delete_album'),
    
    # Following Artists
    path('following', views.followed_artists_list_view, name='following_list'),
    path('following/add', views.follow_artist_view, name='following_add'),
    path('following/<str:id>', views.unfollow_artist_view, name='following_delete'),
    path('following/<str:id>/check', views.artist_release_check_view, name='following_check'),
    
    # Cloud Library
    path('cloud-library/health', views.cloud_library_health_view, name='cloud_library_health'),
    path('cloud-library/albums', views.cloud_library_items_view, name='cloud_library_albums'),
    path('cloud-library/playlists', views.cloud_library_items_view, name='cloud_library_playlists'),
    path('cloud-library/playlists/<str:id>', views.cloud_library_playlist_detail_view, name='cloud_library_playlist_detail'),
    path('cloud-library/songs', views.cloud_library_items_view, name='cloud_library_songs'),
    path('cloud-library/download-all', views.cloud_library_download_all_view, name='cloud_library_download_all'),
    
    # Server-Sent Events
    path('events', views.events_view, name='events'),
]
