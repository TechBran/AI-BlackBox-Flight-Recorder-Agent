package com.aiblackbox.portal.ui.media

import android.app.Application
import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.widget.Toast
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral150
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral400
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.glassSurface
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

// =============================================================================
// Data Model — aligned with Portal API response
// =============================================================================

data class MediaItem(
    val filename: String,
    val url: String,
    val type: String,       // "image", "video", "audio", "other"
    val size: Long = 0,
    val modified: Double = 0.0
)

// =============================================================================
// ViewModel — full featured media management
// =============================================================================

class MediaViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _allItems = MutableStateFlow<List<MediaItem>>(emptyList())
    val allItems: StateFlow<List<MediaItem>> = _allItems.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _deleteInProgress = MutableStateFlow<String?>(null)
    val deleteInProgress: StateFlow<String?> = _deleteInProgress.asStateFlow()

    private var origin = ""

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        this.origin = origin
        api = BlackBoxApi(origin)
        loadMedia()
    }

    fun loadMedia() {
        val api = api ?: return
        _isLoading.value = true
        _error.value = null
        viewModelScope.launch {
            try {
                val response = api.get("/api/media/list?page=1&per_page=1000")
                val root = json.parseToJsonElement(response).jsonObject
                val filesArr = root["files"]?.jsonArray ?: run {
                    _allItems.value = emptyList()
                    return@launch
                }
                _allItems.value = filesArr.map { el ->
                    val obj = el.jsonObject
                    val filename = obj["filename"]?.jsonPrimitive?.content ?: ""
                    val relUrl = obj["url"]?.jsonPrimitive?.content ?: ""
                    val type = obj["type"]?.jsonPrimitive?.content ?: "other"
                    val size = try {
                        obj["size"]?.jsonPrimitive?.long ?: 0L
                    } catch (_: Exception) { 0L }
                    val modified = try {
                        obj["modified"]?.jsonPrimitive?.content?.toDoubleOrNull() ?: 0.0
                    } catch (_: Exception) { 0.0 }
                    MediaItem(
                        filename = filename,
                        url = "$origin$relUrl",
                        type = type,
                        size = size,
                        modified = modified
                    )
                }
            } catch (e: Exception) {
                _error.value = e.message ?: "Failed to load media"
                _allItems.value = emptyList()
            } finally {
                _isLoading.value = false
            }
        }
    }

    fun deleteFile(filename: String) {
        val api = api ?: return
        _deleteInProgress.value = filename
        viewModelScope.launch {
            try {
                api.delete("/api/media/delete/$filename")
                _allItems.value = _allItems.value.filter { it.filename != filename }
            } catch (e: Exception) {
                _error.value = "Delete failed: ${e.message}"
            } finally {
                _deleteInProgress.value = null
            }
        }
    }
}

// =============================================================================
// Main Screen Composable
// =============================================================================

private const val PER_PAGE = 50

@Composable
fun MediaBrowserScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: MediaViewModel = viewModel()
) {
    val view = LocalView.current
    val context = LocalContext.current
    val allItems by viewModel.allItems.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()
    val deleteInProgress by viewModel.deleteInProgress.collectAsState()

    // Local UI state
    var searchQuery by remember { mutableStateOf("") }
    var selectedType by remember { mutableStateOf("") }  // "" = All
    var currentPage by remember { mutableIntStateOf(1) }
    var expandedFilename by remember { mutableStateOf<String?>(null) }
    var deleteConfirmFilename by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Client-side filtering (matches Portal approach)
    val filteredItems by remember(allItems, searchQuery, selectedType) {
        derivedStateOf {
            allItems.filter { item ->
                val matchesSearch = searchQuery.isBlank() ||
                    item.filename.contains(searchQuery, ignoreCase = true)
                val matchesType = selectedType.isBlank() || item.type == selectedType
                matchesSearch && matchesType
            }
        }
    }

    // Pagination
    val totalPages by remember(filteredItems) {
        derivedStateOf { maxOf(1, (filteredItems.size + PER_PAGE - 1) / PER_PAGE) }
    }
    val pagedItems by remember(filteredItems, currentPage) {
        derivedStateOf {
            val startIdx = (currentPage - 1) * PER_PAGE
            val endIdx = minOf(startIdx + PER_PAGE, filteredItems.size)
            if (startIdx < filteredItems.size) filteredItems.subList(startIdx, endIdx)
            else emptyList()
        }
    }

    // Reset page when filters change
    LaunchedEffect(searchQuery, selectedType) { currentPage = 1 }

    // Delete confirmation dialog
    deleteConfirmFilename?.let { filename ->
        AlertDialog(
            onDismissRequest = { deleteConfirmFilename = null },
            containerColor = Neutral200,
            titleContentColor = BbxWhite,
            textContentColor = BbxDim,
            title = { Text("Delete File") },
            text = { Text("Delete \"$filename\"? This cannot be undone.") },
            confirmButton = {
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.deleteFile(filename)
                    deleteConfirmFilename = null
                    if (expandedFilename == filename) expandedFilename = null
                }) {
                    Text("Delete", color = BbxAccent)
                }
            },
            dismissButton = {
                TextButton(onClick = {
                    view.performPressFeedback()
                    deleteConfirmFilename = null
                }) {
                    Text("Cancel", color = BbxDim)
                }
            }
        )
    }

    Column(modifier = modifier.fillMaxSize().padding(start = 16.dp, end = 16.dp, bottom = 12.dp, top = 100.dp)) {
        // ─── Header Row ────────────────────────────────────────────
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text(
                    "Media",
                    style = MaterialTheme.typography.headlineMedium,
                    color = BbxWhite
                )
                Text(
                    buildString {
                        append("${filteredItems.size} files")
                        if (searchQuery.isNotBlank() || selectedType.isNotBlank()) {
                            append(" (filtered)")
                        }
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral700
                )
            }
            IconButton(onClick = {
                view.performPressFeedback()
                viewModel.loadMedia()
            }) {
                Icon(
                    Icons.Default.Refresh,
                    contentDescription = "Refresh",
                    tint = BbxDim
                )
            }
        }

        Spacer(Modifier.height(10.dp))

        // ─── Search Bar ────────────────────────────────────────────
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .glassSurface(shape = RoundedCornerShape(RadiusMd))
                .padding(horizontal = 12.dp, vertical = 10.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Default.Search,
                    contentDescription = "Search",
                    tint = Neutral500,
                    modifier = Modifier.size(18.dp)
                )
                Spacer(Modifier.width(8.dp))
                Box(Modifier.weight(1f)) {
                    if (searchQuery.isEmpty()) {
                        Text(
                            "Search files...",
                            style = MaterialTheme.typography.bodyMedium,
                            color = Neutral500
                        )
                    }
                    BasicTextField(
                        value = searchQuery,
                        onValueChange = { searchQuery = it },
                        textStyle = MaterialTheme.typography.bodyMedium.copy(color = BbxWhite),
                        cursorBrush = SolidColor(BbxAccent),
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth()
                    )
                }
                if (searchQuery.isNotEmpty()) {
                    IconButton(
                        onClick = {
                            view.performPressFeedback()
                            searchQuery = ""
                        },
                        modifier = Modifier.size(24.dp)
                    ) {
                        Icon(
                            Icons.Default.Close,
                            contentDescription = "Clear",
                            tint = Neutral500,
                            modifier = Modifier.size(16.dp)
                        )
                    }
                }
            }
        }

        Spacer(Modifier.height(10.dp))

        // ─── Type Filter Chips ─────────────────────────────────────
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth()
        ) {
            val types = listOf("" to "All", "image" to "Images", "video" to "Videos", "audio" to "Audio")
            types.forEach { (value, label) ->
                FilterChip(
                    selected = selectedType == value,
                    onClick = {
                        view.performPressFeedback()
                        selectedType = value
                    },
                    label = {
                        Text(
                            label,
                            style = MaterialTheme.typography.labelMedium
                        )
                    },
                    colors = FilterChipDefaults.filterChipColors(
                        containerColor = Neutral150,
                        labelColor = Neutral700,
                        selectedContainerColor = BbxAccent.copy(alpha = 0.15f),
                        selectedLabelColor = BbxAccent
                    ),
                    border = FilterChipDefaults.filterChipBorder(
                        borderColor = Neutral300,
                        selectedBorderColor = BbxAccent.copy(alpha = 0.4f),
                        enabled = true,
                        selected = selectedType == value
                    )
                )
            }
        }

        Spacer(Modifier.height(10.dp))

        // ─── Loading / Error States ────────────────────────────────
        if (isLoading) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .padding(vertical = 24.dp),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(
                    color = BbxAccent,
                    modifier = Modifier.size(28.dp),
                    strokeWidth = 2.dp
                )
            }
        }

        error?.let { msg ->
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(BbxAccent.copy(alpha = 0.1f))
                    .padding(12.dp)
            ) {
                Text(
                    "Failed to load media: $msg",
                    style = MaterialTheme.typography.bodySmall,
                    color = BbxAccent
                )
            }
            Spacer(Modifier.height(8.dp))
        }

        // ─── Media List ────────────────────────────────────────────
        if (!isLoading && pagedItems.isEmpty() && error == null) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .padding(vertical = 48.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    "No media files found",
                    style = MaterialTheme.typography.bodyMedium,
                    color = Neutral500
                )
            }
        }

        LazyColumn(
            modifier = Modifier.weight(1f),
            contentPadding = PaddingValues(vertical = 4.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            items(pagedItems, key = { it.filename }) { item ->
                MediaListItem(
                    item = item,
                    isExpanded = expandedFilename == item.filename,
                    isDeleting = deleteInProgress == item.filename,
                    onToggleExpand = {
                        expandedFilename = if (expandedFilename == item.filename) null
                        else item.filename
                    },
                    onDownload = {
                        downloadFile(context, item.url, item.filename)
                    },
                    onDelete = {
                        deleteConfirmFilename = item.filename
                    }
                )
            }
        }

        // ─── Pagination Controls ───────────────────────────────────
        if (totalPages > 1) {
            Spacer(Modifier.height(8.dp))
            Row(
                Modifier
                    .fillMaxWidth()
                    .glassSurface(shape = RoundedCornerShape(RadiusMd))
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(
                    onClick = {
                        view.performPressFeedback()
                        if (currentPage > 1) currentPage--
                    },
                    enabled = currentPage > 1,
                    modifier = Modifier.size(36.dp)
                ) {
                    Icon(
                        Icons.AutoMirrored.Filled.ArrowBack,
                        contentDescription = "Previous page",
                        tint = if (currentPage > 1) BbxDim else Neutral400,
                        modifier = Modifier.size(18.dp)
                    )
                }
                Text(
                    "Page $currentPage of $totalPages",
                    style = MaterialTheme.typography.labelMedium,
                    color = Neutral700
                )
                IconButton(
                    onClick = {
                        view.performPressFeedback()
                        if (currentPage < totalPages) currentPage++
                    },
                    enabled = currentPage < totalPages,
                    modifier = Modifier.size(36.dp)
                ) {
                    Icon(
                        Icons.AutoMirrored.Filled.ArrowForward,
                        contentDescription = "Next page",
                        tint = if (currentPage < totalPages) BbxDim else Neutral400,
                        modifier = Modifier.size(18.dp)
                    )
                }
            }
        }
    }
}

// =============================================================================
// Media List Item — expandable accordion (matches Portal .media-list-item)
// =============================================================================

@Composable
private fun MediaListItem(
    item: MediaItem,
    isExpanded: Boolean,
    isDeleting: Boolean,
    onToggleExpand: () -> Unit,
    onDownload: () -> Unit,
    onDelete: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .animateContentSize()
            .glassSurface(shape = RoundedCornerShape(RadiusMd))
    ) {
        // ─── Header (always visible) ──────────────────────────────
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickFeedback { onToggleExpand() }
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // File type icon
            Text(
                when (item.type) {
                    "image" -> "\uD83D\uDDBC\uFE0F"   // framed picture
                    "video" -> "\uD83C\uDFAC"          // clapper board
                    "audio" -> "\uD83C\uDFB5"          // musical note
                    else -> "\uD83D\uDCC4"             // page
                },
                style = MaterialTheme.typography.titleLarge
            )
            Spacer(Modifier.width(12.dp))

            // File info
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    item.filename,
                    style = MaterialTheme.typography.bodyMedium,
                    color = BbxWhite,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    "${formatFileSize(item.size)} \u2022 ${formatDate(item.modified)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral700
                )
            }

            // Type badge
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(typeColor(item.type).copy(alpha = 0.15f))
                    .padding(horizontal = 8.dp, vertical = 3.dp)
            ) {
                Text(
                    item.type,
                    style = MaterialTheme.typography.labelSmall,
                    color = typeColor(item.type)
                )
            }

            Spacer(Modifier.width(8.dp))

            // Expand chevron
            Icon(
                if (isExpanded) Icons.Default.KeyboardArrowUp
                else Icons.Default.KeyboardArrowDown,
                contentDescription = if (isExpanded) "Collapse" else "Expand",
                tint = Neutral500,
                modifier = Modifier.size(20.dp)
            )
        }

        // ─── Expanded Content ─────────────────────────────────────
        AnimatedVisibility(
            visible = isExpanded,
            enter = expandVertically(),
            exit = shrinkVertically()
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Neutral100)
                    .padding(12.dp)
            ) {
                // Preview area
                when (item.type) {
                    "image" -> {
                        AsyncImage(
                            model = item.url,
                            contentDescription = item.filename,
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(200.dp)
                                .clip(RoundedCornerShape(RadiusSm)),
                            contentScale = ContentScale.Fit
                        )
                    }
                    "video" -> {
                        // Inline video player using AndroidView + VideoView
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(220.dp)
                                .clip(RoundedCornerShape(RadiusSm))
                                .background(Neutral100)
                        ) {
                            androidx.compose.runtime.key(item.url) {
                                androidx.compose.ui.viewinterop.AndroidView(
                                    modifier = Modifier.fillMaxSize(),
                                    factory = { ctx ->
                                        android.widget.VideoView(ctx).apply {
                                            setVideoURI(Uri.parse(item.url))
                                            val mc = android.widget.MediaController(ctx)
                                            mc.setAnchorView(this)
                                            setMediaController(mc)
                                            setOnPreparedListener { mp ->
                                                mp.isLooping = false
                                                // Seek to 100ms to show first frame as thumbnail
                                                seekTo(100)
                                            }
                                            setOnErrorListener { _, _, _ ->
                                                android.util.Log.e("MediaBrowser", "Video error: ${item.url}")
                                                false
                                            }
                                            // Do NOT set background color — it covers the video surface
                                        }
                                    }
                                )
                            }
                        }
                    }
                    "audio" -> {
                        // Reuse AudioPlayerBar component for inline playback
                        com.aiblackbox.portal.ui.components.AudioPlayerBar(
                            audioUrl = item.url,
                            modifier = Modifier.fillMaxWidth()
                        )
                    }
                    else -> {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(80.dp)
                                .clip(RoundedCornerShape(RadiusSm))
                                .background(Neutral200),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                "\uD83D\uDCC4 ${item.filename}",
                                style = MaterialTheme.typography.bodyMedium,
                                color = Neutral500
                            )
                        }
                    }
                }

                Spacer(Modifier.height(12.dp))

                // Action buttons
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    // Download button
                    Row(
                        modifier = Modifier
                            .weight(1f)
                            .clip(RoundedCornerShape(RadiusSm))
                            .background(BbxAccent.copy(alpha = 0.12f))
                            .clickFeedback { onDownload() }
                            .padding(vertical = 10.dp),
                        horizontalArrangement = Arrangement.Center,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            "\u2B07\uFE0F",
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "Download",
                            style = MaterialTheme.typography.labelLarge,
                            color = BbxAccent
                        )
                    }

                    // Delete button
                    Row(
                        modifier = Modifier
                            .weight(1f)
                            .clip(RoundedCornerShape(RadiusSm))
                            .background(Neutral200)
                            .clickFeedback { onDelete() }
                            .padding(vertical = 10.dp),
                        horizontalArrangement = Arrangement.Center,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        if (isDeleting) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(14.dp),
                                color = BbxAccent,
                                strokeWidth = 2.dp
                            )
                        } else {
                            Icon(
                                Icons.Default.Delete,
                                contentDescription = "Delete",
                                tint = Neutral700,
                                modifier = Modifier.size(16.dp)
                            )
                        }
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "Delete",
                            style = MaterialTheme.typography.labelLarge,
                            color = Neutral700
                        )
                    }
                }
            }
        }
    }
}

// =============================================================================
// Utility Functions — matching Portal JS helpers
// =============================================================================

private fun typeColor(type: String) = when (type) {
    "image" -> BbxAccent
    "video" -> androidx.compose.ui.graphics.Color(0xFF4DD0E1)  // cyan
    "audio" -> androidx.compose.ui.graphics.Color(0xFFBB86FC)  // purple
    else -> Neutral700
}

private fun formatFileSize(bytes: Long): String = when {
    bytes <= 0 -> "0 B"
    bytes < 1024 -> "$bytes B"
    bytes < 1024 * 1024 -> "${"%.1f".format(bytes / 1024.0)} KB"
    bytes < 1024L * 1024 * 1024 -> "${"%.1f".format(bytes / (1024.0 * 1024))} MB"
    else -> "${"%.1f".format(bytes / (1024.0 * 1024 * 1024))} GB"
}

private fun formatDate(timestamp: Double): String {
    if (timestamp <= 0) return "Unknown date"
    val millis = (timestamp * 1000).toLong()
    val diff = System.currentTimeMillis() - millis
    val days = diff / (1000 * 60 * 60 * 24)
    return when {
        days == 0L -> "Today"
        days == 1L -> "Yesterday"
        days < 7 -> "$days days ago"
        else -> {
            val sdf = java.text.SimpleDateFormat("MMM d", java.util.Locale.US)
            sdf.format(java.util.Date(millis))
        }
    }
}

private fun downloadFile(context: Context, url: String, filename: String) {
    try {
        val request = DownloadManager.Request(Uri.parse(url))
            .setTitle(filename)
            .setDescription("Downloading $filename")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
        val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        dm.enqueue(request)
        Toast.makeText(context, "Downloading $filename", Toast.LENGTH_SHORT).show()
    } catch (e: Exception) {
        Toast.makeText(context, "Download failed: ${e.message}", Toast.LENGTH_SHORT).show()
    }
}
