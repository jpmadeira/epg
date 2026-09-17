# Times Brasil CNBC - EPG automático

Este projeto gera XMLTV automaticamente a partir da grade do Line-UP do canal 8296.

## Endpoint

Depois do deploy:

`https://SEU-PROJETO.vercel.app/epg.xml`

Essa é a URL para colocar no TiviMate.

## O que é automático

- nome dos programas;
- horários;
- datas;
- programação encontrada para os próximos 7 dias;
- cálculo do horário de término usando o próximo programa.

O código não contém uma lista fixa de programas.

## Deploy

Importe este projeto na Vercel com Framework Preset `Other`.

Não é necessário adicionar runtime Python no `vercel.json`: arquivos `.py` dentro de `api/` são detectados pelo runtime Python oficial da Vercel.
