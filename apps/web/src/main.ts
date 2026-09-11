import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { applyTheme, readStoredTheme } from "./lib/theme";
import { router } from "./router";
import "./styles.css";
import "markstream-vue/index.css";

applyTheme(readStoredTheme());
createApp(App).use(createPinia()).use(router).mount("#app");
