from django.db import models

class Job(models.Model):
    id = models.CharField(max_length=36, primary_key=True) # UUID
    kind = models.CharField(max_length=20, default='album') # 'album', 'song', 'playlist'
    status = models.CharField(max_length=20, default='pending') # 'pending', 'downloading', 'transcoding', 'moving', 'completed', 'failed', 'cancelled'
    progress = models.IntegerField(default=0)
    
    album_id = models.CharField(max_length=50, blank=True, null=True)
    song_id = models.CharField(max_length=50, blank=True, null=True)
    playlist_id = models.CharField(max_length=100, blank=True, null=True)
    library_playlist_id = models.CharField(max_length=100, blank=True, null=True)
    
    album_title = models.CharField(max_length=255, default='Unknown Album')
    artist = models.CharField(max_length=255, default='Unknown Artist')
    artist_id = models.CharField(max_length=50, blank=True, null=True)
    artwork_url = models.TextField(blank=True, null=True)
    current_track = models.CharField(max_length=255, blank=True, null=True)
    
    message = models.TextField(blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    cancelled = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    final_dir = models.TextField(blank=True, null=True)
    stats = models.JSONField(default=dict, blank=True)
    logs = models.TextField(default='', blank=True)

    def __str__(self):
        return f"{self.kind} - {self.album_title} ({self.status})"

class FollowedArtist(models.Model):
    id = models.CharField(max_length=50, primary_key=True) # Apple Music Artist ID
    name = models.CharField(max_length=255)
    genre_names = models.JSONField(default=list, blank=True)
    url = models.TextField(blank=True, null=True)
    artwork_template = models.TextField(blank=True, null=True)
    artwork_color = models.CharField(max_length=20, blank=True, null=True)
    
    known_release_ids = models.JSONField(default=list, blank=True)
    latest_release_date = models.CharField(max_length=20, blank=True, null=True)
    last_checked_at = models.DateTimeField(blank=True, null=True)
    
    total_release_count = models.IntegerField(default=0)
    missing_release_count = models.IntegerField(default=0)
    release_scope = models.CharField(max_length=20, default='all') # 'all', 'lp', 'ep', 'singles'

    def __str__(self):
        return self.name

class AppSetting(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    value = models.TextField() # Stores JSON serialized string

    def __str__(self):
        return self.key
