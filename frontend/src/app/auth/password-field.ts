import { Component, input, model, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

/**
 * A password field you can actually read back.
 *
 * Not a nicety. A password typed once, never shown, and then required exactly
 * is a trap — and it sprang: the first password set on this app was lost to a
 * typo nobody could see. Hiding input protects against someone reading your
 * screen, which on a laptop you are already sitting in front of is usually
 * nobody.
 *
 * The toggle is a button rather than a checkbox so it never receives focus
 * from tabbing between fields, and it carries `aria-pressed` so a screen
 * reader announces the current state rather than just the label.
 */
@Component({
  selector: 'app-password-field',
  imports: [FormsModule],
  template: `
    <div class="pw">
      <input
        [attr.id]="inputId()"
        [type]="revealed() ? 'text' : 'password'"
        [attr.name]="name()"
        [attr.autocomplete]="autocomplete()"
        [attr.aria-label]="label()"
        [ngModel]="value()"
        (ngModelChange)="value.set($event)"
        required
      />
      <button
        type="button"
        class="reveal"
        (click)="revealed.set(!revealed())"
        [attr.aria-pressed]="revealed()"
        [attr.aria-label]="revealed() ? 'Hide password' : 'Show password'"
        tabindex="-1"
      >
        {{ revealed() ? 'Hide' : 'Show' }}
      </button>
    </div>
  `,
  styles: `
    .pw {
      position: relative;
      display: flex;
      align-items: center;
    }

    input {
      width: 100%;
      /* Room for the button, so a long password never runs under it. */
      padding-right: 3.6rem;
    }

    .reveal {
      position: absolute;
      right: 0.35rem;
      background: none;
      border: none;
      color: var(--text-dim);
      font-size: 12px;
      padding: 0.25rem 0.45rem;
      border-radius: 5px;
      cursor: pointer;

      &:hover {
        color: var(--text);
        background: var(--surface-2);
      }
    }
  `,
})
export class PasswordField {
  value = model<string>('');
  inputId = input<string>('');
  name = input<string>('password');
  autocomplete = input<string>('current-password');
  label = input<string>('Password');

  revealed = signal(false);
}
