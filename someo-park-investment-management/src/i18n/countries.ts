/**
 * Country-name translations for the 48 World Cup 2026 teams (en/zh/ja/fr/es).
 * Keyed by the English name exactly as it appears in the prediction_market data
 * (worldcup_model.json / xv_matches.json / upcoming.json). tCountry() returns the
 * name in the active i18n language, falling back to English for any unknown name.
 */
import i18n from '../i18n';

type Tr = { zh: string; ja: string; fr: string; es: string };

const COUNTRIES: Record<string, Tr> = {
  'Algeria': { zh: '阿尔及利亚', ja: 'アルジェリア', fr: 'Algérie', es: 'Argelia' },
  'Argentina': { zh: '阿根廷', ja: 'アルゼンチン', fr: 'Argentine', es: 'Argentina' },
  'Australia': { zh: '澳大利亚', ja: 'オーストラリア', fr: 'Australie', es: 'Australia' },
  'Austria': { zh: '奥地利', ja: 'オーストリア', fr: 'Autriche', es: 'Austria' },
  'Belgium': { zh: '比利时', ja: 'ベルギー', fr: 'Belgique', es: 'Bélgica' },
  'Bosnia and Herzegovina': { zh: '波黑', ja: 'ボスニア・ヘルツェゴビナ', fr: 'Bosnie-Herzégovine', es: 'Bosnia y Herzegovina' },
  'Brazil': { zh: '巴西', ja: 'ブラジル', fr: 'Brésil', es: 'Brasil' },
  'Canada': { zh: '加拿大', ja: 'カナダ', fr: 'Canada', es: 'Canadá' },
  'Cape Verde': { zh: '佛得角', ja: 'カーボベルデ', fr: 'Cap-Vert', es: 'Cabo Verde' },
  'Colombia': { zh: '哥伦比亚', ja: 'コロンビア', fr: 'Colombie', es: 'Colombia' },
  "Cote d'Ivoire": { zh: '科特迪瓦', ja: 'コートジボワール', fr: "Côte d'Ivoire", es: 'Costa de Marfil' },
  'Croatia': { zh: '克罗地亚', ja: 'クロアチア', fr: 'Croatie', es: 'Croacia' },
  'Curacao': { zh: '库拉索', ja: 'キュラソー', fr: 'Curaçao', es: 'Curazao' },
  'Czechia': { zh: '捷克', ja: 'チェコ', fr: 'Tchéquie', es: 'Chequia' },
  'DR Congo': { zh: '刚果（金）', ja: 'コンゴ民主共和国', fr: 'RD Congo', es: 'RD Congo' },
  'Ecuador': { zh: '厄瓜多尔', ja: 'エクアドル', fr: 'Équateur', es: 'Ecuador' },
  'Egypt': { zh: '埃及', ja: 'エジプト', fr: 'Égypte', es: 'Egipto' },
  'England': { zh: '英格兰', ja: 'イングランド', fr: 'Angleterre', es: 'Inglaterra' },
  'France': { zh: '法国', ja: 'フランス', fr: 'France', es: 'Francia' },
  'Germany': { zh: '德国', ja: 'ドイツ', fr: 'Allemagne', es: 'Alemania' },
  'Ghana': { zh: '加纳', ja: 'ガーナ', fr: 'Ghana', es: 'Ghana' },
  'Haiti': { zh: '海地', ja: 'ハイチ', fr: 'Haïti', es: 'Haití' },
  'Iran': { zh: '伊朗', ja: 'イラン', fr: 'Iran', es: 'Irán' },
  'Iraq': { zh: '伊拉克', ja: 'イラク', fr: 'Irak', es: 'Irak' },
  'Japan': { zh: '日本', ja: '日本', fr: 'Japon', es: 'Japón' },
  'Jordan': { zh: '约旦', ja: 'ヨルダン', fr: 'Jordanie', es: 'Jordania' },
  'Korea Republic': { zh: '韩国', ja: '韓国', fr: 'Corée du Sud', es: 'Corea del Sur' },
  'Mexico': { zh: '墨西哥', ja: 'メキシコ', fr: 'Mexique', es: 'México' },
  'Morocco': { zh: '摩洛哥', ja: 'モロッコ', fr: 'Maroc', es: 'Marruecos' },
  'Netherlands': { zh: '荷兰', ja: 'オランダ', fr: 'Pays-Bas', es: 'Países Bajos' },
  'New Zealand': { zh: '新西兰', ja: 'ニュージーランド', fr: 'Nouvelle-Zélande', es: 'Nueva Zelanda' },
  'Norway': { zh: '挪威', ja: 'ノルウェー', fr: 'Norvège', es: 'Noruega' },
  'Panama': { zh: '巴拿马', ja: 'パナマ', fr: 'Panama', es: 'Panamá' },
  'Paraguay': { zh: '巴拉圭', ja: 'パラグアイ', fr: 'Paraguay', es: 'Paraguay' },
  'Portugal': { zh: '葡萄牙', ja: 'ポルトガル', fr: 'Portugal', es: 'Portugal' },
  'Qatar': { zh: '卡塔尔', ja: 'カタール', fr: 'Qatar', es: 'Catar' },
  'Saudi Arabia': { zh: '沙特', ja: 'サウジアラビア', fr: 'Arabie saoudite', es: 'Arabia Saudí' },
  'Scotland': { zh: '苏格兰', ja: 'スコットランド', fr: 'Écosse', es: 'Escocia' },
  'Senegal': { zh: '塞内加尔', ja: 'セネガル', fr: 'Sénégal', es: 'Senegal' },
  'South Africa': { zh: '南非', ja: '南アフリカ', fr: 'Afrique du Sud', es: 'Sudáfrica' },
  'Spain': { zh: '西班牙', ja: 'スペイン', fr: 'Espagne', es: 'España' },
  'Sweden': { zh: '瑞典', ja: 'スウェーデン', fr: 'Suède', es: 'Suecia' },
  'Switzerland': { zh: '瑞士', ja: 'スイス', fr: 'Suisse', es: 'Suiza' },
  'Tunisia': { zh: '突尼斯', ja: 'チュニジア', fr: 'Tunisie', es: 'Túnez' },
  'Turkey': { zh: '土耳其', ja: 'トルコ', fr: 'Turquie', es: 'Turquía' },
  'United States': { zh: '美国', ja: 'アメリカ', fr: 'États-Unis', es: 'Estados Unidos' },
  'Uruguay': { zh: '乌拉圭', ja: 'ウルグアイ', fr: 'Uruguay', es: 'Uruguay' },
  'Uzbekistan': { zh: '乌兹别克斯坦', ja: 'ウズベキスタン', fr: 'Ouzbékistan', es: 'Uzbekistán' },
};

/** Country name in the active language (English fallback for unknown / 'en'). */
export function tCountry(name?: string | null): string {
  if (!name) return '';
  const lang = (i18n.language || 'en').slice(0, 2) as keyof Tr;
  if (lang === 'zh' || lang === 'ja' || lang === 'fr' || lang === 'es') {
    return COUNTRIES[name]?.[lang] ?? name;
  }
  return name;
}
