package place.wong.shrimp.companion.ui.theme

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

private val LightColors = lightColorScheme(
    primary = Color(0xFF2457D6),
    secondary = Color(0xFF5D6B98),
    tertiary = Color(0xFF006B5F),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFB0C6FF),
    secondary = Color(0xFFC2C9E9),
    tertiary = Color(0xFF53DBC9),
)

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun CompanionTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> DarkColors
        else -> LightColors
    }
    MaterialExpressiveTheme(colorScheme = colors, content = content)
}
